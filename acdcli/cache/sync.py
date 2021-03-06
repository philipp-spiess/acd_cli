"""
Syncs Amazon Node API objects with SQLite database.
"""

import logging
from datetime import datetime
from itertools import islice

from acdcli.cache.query import Node
from .cursors import mod_cursor
import dateutil.parser as iso_date

logger = logging.getLogger(__name__)


# prevent sqlite3 from throwing too many arguments errors (#145)
def gen_slice(list_, length=100):
    it = iter(list_)
    while True:
        slice_ = [_ for _ in islice(it, length)]
        if not slice_:
            return
        yield slice_


def placeholders(args):
    return '(%s)' % ','.join('?' * len(args))


class SyncMixin(object):
    """Sync mixin to the :class:`NodeCache <acdcli.cache.db.NodeCache>`"""

    def remove_purged(self, purged: list):
        """Removes purged nodes from database

        :param purged: list of purged node IDs"""

        if not purged:
            return

        for slice_ in gen_slice(purged):
            with mod_cursor(self._conn) as c:
                c.execute('DELETE FROM nodes WHERE id IN %s' % placeholders(slice_), slice_)
                c.execute('DELETE FROM files WHERE id IN %s' % placeholders(slice_), slice_)
                c.execute('DELETE FROM content WHERE id IN %s' % placeholders(slice_), slice_)
                c.execute('DELETE FROM parentage WHERE parent IN %s' % placeholders(slice_), slice_)
                c.execute('DELETE FROM parentage WHERE child IN %s' % placeholders(slice_), slice_)
                c.execute('DELETE FROM properties WHERE id IN %s' % placeholders(slice_), slice_)
                c.execute('DELETE FROM labels WHERE id IN %s' % placeholders(slice_), slice_)

        logger.info('Purged %i node(s).' % len(purged))

    def resolve_cache_add(self, path:str, node_id:str):
        with self.node_cache_lock:
            self.path_to_node_id_cache[path] = node_id

    def resolve_cache_del(self, path:str):
        with self.node_cache_lock:
            try: del self.path_to_node_id_cache[path]
            except:pass

    def insert_nodes(self, nodes: list, partial:bool=True, flush_resolve_cache:bool=False):
        """Inserts mixed list of files and folders into cache."""

        if flush_resolve_cache:
            with self.node_cache_lock:
                self.path_to_node_id_cache.clear()

        files = []
        folders = []
        for node in nodes:
            if node['status'] == 'PENDING':
                continue
            kind = node['kind']
            if kind == 'FILE':
                if not 'name' in node or not node['name']:
                    logger.warning('Skipping file %s because its name is empty.' % node['id'])
                    continue
                files.append(node)
            elif kind == 'FOLDER':
                if (not 'name' in node or not node['name']) \
                and (not 'isRoot' in node or not node['isRoot']):
                    logger.warning('Skipping non-root folder %s because its name is empty.'
                                   % node['id'])
                    continue
                folders.append(node)
            elif kind != 'ASSET':
                logger.warning('Cannot insert unknown node type "%s".' % kind)
        self.insert_folders(folders)
        self.insert_files(files)

        self.insert_parentage(files + folders, partial)
        self.insert_properties(files + folders)

    def insert_node(self, node:dict, flush_resolve_cache:bool=False):
        """Inserts single file or folder into cache."""
        if not node:
            return
        self.insert_nodes([node], flush_resolve_cache=flush_resolve_cache)

    def insert_folders(self, folders: list):
        """ Inserts list of folders into cache. Sets 'update' column to current date.

        :param folders: list of raw dict-type folders"""

        if not folders:
            return

        with mod_cursor(self._conn) as c:
            for f in folders:
                n = Node(dict(id=f['id'],
                              type="folder",
                              name=f.get('name'),
                              description=f.get('description'),
                              created=iso_date.parse(f['createdDate']),
                              modified=iso_date.parse(f['modifiedDate']),
                              updated=datetime.utcnow(),
                              status=f['status'],
                              md5=None,
                              size=0,
                              version=0,
                              ))

                with self.node_cache_lock:
                    if n.is_available:
                        self.node_id_to_node_cache[n.id] = n
                    else:
                        try: del self.node_id_to_node_cache[n.id]
                        except: pass

                c.execute(
                    'INSERT OR REPLACE INTO nodes '
                    '(id, type, name, description, created, modified, updated, status) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    [n.id, n.type, n.name, n.description,
                     n.created, n.modified,
                     n.updated,
                     n.status
                     ]
                )

        logger.info('Inserted/updated %d folder(s).' % len(folders))

    def insert_files(self, files: list):
        if not files:
            return

        with mod_cursor(self._conn) as c:
            for f in files:
                n = Node(dict(id=f['id'],
                              type="file",
                              name=f.get('name'),
                              description=f.get('description'),
                              created=iso_date.parse(f['createdDate']),
                              modified=iso_date.parse(f['modifiedDate']),
                              updated=datetime.utcnow(),
                              status=f['status'],
                              md5=f.get('contentProperties', {}).get('md5', 'd41d8cd98f00b204e9800998ecf8427e'),
                              size=f.get('contentProperties', {}).get('size', 0),
                              version=f.get('contentProperties', {}).get('version', 0),
                              ))

                with self.node_cache_lock:
                    if n.is_available:
                        self.node_id_to_node_cache[n.id] = n
                    else:
                        try: del self.node_id_to_node_cache[n.id]
                        except: pass

                if not n.is_available:
                    self.remove_content(n.id)

                c.execute(
                    'INSERT OR REPLACE INTO nodes '
                    '(id, type, name, description, created, modified, updated, status) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    [n.id, n.type, n.name, n.description,
                     n.created, n.modified,
                     n.updated,
                     n.status
                     ]
                )
                c.execute(
                    'INSERT OR REPLACE INTO files (id, md5, size, version) VALUES (?, ?, ?, ?)',
                    [n.id,
                     n.md5,
                     n.size,
                     n.version,
                     ]
                )

        logger.info('Inserted/updated %d file(s).' % len(files))

    def insert_parentage(self, nodes: list, partial=True):
        if not nodes:
            return

        if partial:
            with mod_cursor(self._conn) as c:
                for slice_ in gen_slice(nodes):
                    c.execute('DELETE FROM parentage WHERE child IN %s' % placeholders(slice_),
                              [n['id'] for n in slice_])

        with mod_cursor(self._conn) as c:
            for n in nodes:
                for p in n['parents']:
                    c.execute('INSERT OR IGNORE INTO parentage VALUES (?, ?)', [p, n['id']])

        logger.info('Parented %d node(s).' % len(nodes))

    def insert_properties(self, nodes: list):
        if not nodes:
            return

        with mod_cursor(self._conn) as c:
            for n in nodes:
                if 'properties' not in n:
                    continue
                id = n['id']
                for owner_id, key_value in n['properties'].items():
                    for key, value in key_value.items():
                        c.execute('INSERT OR REPLACE INTO properties '
                                  '(id, owner, key, value) '
                                  'VALUES (?, ?, ?, ?)',
                                  [id, owner_id, key, value]
                                  )

        logger.info('Applied properties to %d node(s).' % len(nodes))

    def insert_property(self, node_id, owner_id, key, value):
        with mod_cursor(self._conn) as c:
            c.execute('INSERT OR REPLACE INTO properties '
                      '(id, owner, key, value) '
                      'VALUES (?, ?, ?, ?)',
                      [node_id, owner_id, key, value]
                      )

    def insert_content(self, node_id:str, version:int, value:bytes):
        with mod_cursor(self._conn) as c:
            c.execute('INSERT OR REPLACE INTO content '
                      '(id, value, size, version, accessed) '
                      'VALUES (?, ?, ?, ?, ?)',
                      [node_id, value, len(value), version, datetime.utcnow()]
                      )

    def remove_content(self, node_id:str):
        with mod_cursor(self._conn) as c:
            c.execute('DELETE FROM content WHERE id=?',
                      [node_id]
                      )
