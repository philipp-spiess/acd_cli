import logging
from sqlite3 import OperationalError
from .cursors import *

logger = logging.getLogger(__name__)

# _KeyValueStorage


_CREATION_SCRIPT = """
    CREATE TABLE metadata (
        "key" VARCHAR(64) NOT NULL,
        value VARCHAR,
        PRIMARY KEY ("key")
    );

    CREATE TABLE nodes (
        id VARCHAR(50) NOT NULL,
        type VARCHAR(15),
        name VARCHAR(256),
        description VARCHAR(500),
        created DATETIME,
        modified DATETIME,
        updated DATETIME,
        status VARCHAR(9),
        PRIMARY KEY (id),
        UNIQUE (id),
        CHECK (status IN ('AVAILABLE', 'TRASH', 'PURGED', 'PENDING'))
    );

    CREATE TABLE properties (
        id VARCHAR(50) NOT NULL,
        owner TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY (id, owner, key),
        FOREIGN KEY(id) REFERENCES nodes (id)
    );

    CREATE TABLE labels (
        id VARCHAR(50) NOT NULL,
        name VARCHAR(256) NOT NULL,
        PRIMARY KEY (id, name),
        FOREIGN KEY(id) REFERENCES nodes (id)
    );

    CREATE TABLE files (
        id VARCHAR(50) NOT NULL,
        md5 VARCHAR(32),
        size BIGINT,
        version BIGINT,
        PRIMARY KEY (id),
        UNIQUE (id),
        FOREIGN KEY(id) REFERENCES nodes (id)
    );

    CREATE TABLE parentage (
        parent VARCHAR(50) NOT NULL,
        child VARCHAR(50) NOT NULL,
        PRIMARY KEY (parent, child),
        FOREIGN KEY(parent) REFERENCES nodes (id),
        FOREIGN KEY(child) REFERENCES nodes (id)
    );

    CREATE TABLE content (
        id VARCHAR(50) NOT NULL,
        value BLOB,
        size BIGINT,
        version BIGINT,
        accessed DATETIME,
        PRIMARY KEY (id),
        UNIQUE (id),
        FOREIGN KEY(id) REFERENCES nodes (id)
    );

    CREATE INDEX ix_content_size ON content(size);
    CREATE INDEX ix_content_accessed ON content(accessed);
    CREATE INDEX ix_parentage_child ON parentage(child);
    CREATE INDEX ix_parentage_parent ON parentage(parent);
    CREATE INDEX ix_nodes_names ON nodes(name);
    PRAGMA user_version = 4;
    """

_GEN_DROP_TABLES_SQL = \
    'SELECT "DROP TABLE " || name || ";" FROM sqlite_master WHERE type == "table"'


def _0_to_1(conn):
    conn.executescript(
        'ALTER TABLE nodes ADD updated DATETIME;'
        'ALTER TABLE nodes ADD description VARCHAR(500);'
        'PRAGMA user_version = 1;'
    )
    conn.commit()


def _1_to_2(conn):
    conn.executescript(
        'DROP TABLE IF EXISTS folders;'
        'CREATE INDEX IF NOT EXISTS ix_nodes_names ON nodes(name);'
        'REINDEX;'
        'PRAGMA user_version = 2;'
    )
    conn.commit()


def _2_to_3(conn):
    conn.executescript(
        # For people upgrading from the main branch to PR374, this line should make the db queries work.
        # The user would also need to old-sync if they had multiple databases *and* were all ready using
        # properties in some of them. It's not clear how to do that from here aside from dropping all data.
        'CREATE TABLE IF NOT EXISTS properties (id VARCHAR(50) NOT NULL, owner TEXT NOT NULL, '
        'key TEXT NOT NULL, value TEXT, PRIMARY KEY (id, owner, key), FOREIGN KEY(id) REFERENCES nodes (id));'

        'CREATE INDEX IF NOT EXISTS ix_parentage_child ON parentage(child);'
        'CREATE INDEX IF NOT EXISTS ix_parentage_parent ON parentage(parent);'
        # Having changed the schema, the queries can be optimised differently.
        # In order to be aware of that, re-analyze the type of data and indexes,
        # allowing SQLite3 to make better decisions.
        'ANALYZE;'
        'PRAGMA user_version = 3;'
    )
    conn.commit()


def _3_to_4(conn):
    conn.executescript(
        'ALTER TABLE files ADD version BIGINT;'

        'DROP TABLE IF EXISTS content;'
        'CREATE TABLE content (id VARCHAR(50) NOT NULL, value BLOB, size BIGINT, version BIGINT, accessed DATETIME,'
        'PRIMARY KEY (id), UNIQUE (id), FOREIGN KEY(id) REFERENCES nodes (id)); '

        'CREATE INDEX IF NOT EXISTS ix_content_size ON content(size);'
        'CREATE INDEX IF NOT EXISTS ix_content_accessed ON content(accessed);'
        # Having changed the schema, the queries can be optimised differently.
        # In order to be aware of that, re-analyze the type of data and indexes,
        # allowing SQLite3 to make better decisions.
        'ANALYZE;'
        'PRAGMA user_version = 4;'
    )
    conn.commit()

_migrations = [_0_to_1, _1_to_2, _2_to_3, _3_to_4]
"""list of all migrations from index -> index+1"""


class SchemaMixin(object):
    _DB_SCHEMA_VER = 4

    def init(self):
        try:
            self.create_tables()
        except OperationalError:
            pass
        with cursor(self._conn) as c:
            c.execute('PRAGMA user_version;')
            r = c.fetchone()
        ver = r[0]

        logger.info('DB schema version is %i.' % ver)

        if self._DB_SCHEMA_VER > ver:
            self._migrate(ver)

        self.KeyValueStorage = _KeyValueStorage(self._conn)

    def create_tables(self):
        self._conn.executescript(_CREATION_SCRIPT)
        self._conn.commit()

    def _migrate(self, version):
        for i, migration in enumerate(_migrations[version:]):
            v = i + version
            logger.info('Migrating from schema version %i to %i' % (v, v + 1))
            migration(self._conn)

    def drop_all(self):
        drop_sql = []
        with cursor(self._conn) as c:
            c.execute(_GEN_DROP_TABLES_SQL)
            dt = c.fetchone()
            while dt:
                drop_sql.append(dt[0])
                dt = c.fetchone()

        with mod_cursor(self._conn) as c:
            for drop in drop_sql:
                c.execute(drop)
        self._conn.commit()
        logger.info('Dropped all tables.')
        return True


class _KeyValueStorage(object):
    def __init__(self, conn):
        self.conn = conn

    def __getitem__(self, key: str):
        with cursor(self.conn) as c:
            c.execute('SELECT value FROM metadata WHERE key = (?)', [key])
            r = c.fetchone()
        if r:
            return r['value']
        else:
            raise KeyError

    def __setitem__(self, key: str, value: str):
        with mod_cursor(self.conn) as c:
            c.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', [key, value])

    # def __len__(self):
    #     return self.Session.query(Metadate).count()

    def get(self, key: str, default: str = None):
        with cursor(self.conn) as c:
            c.execute('SELECT value FROM metadata WHERE key == ?', [key])
            r = c.fetchone()

        return r['value'] if r else default

    def update(self, dict_: dict):
        for key in dict_.keys():
            self.__setitem__(key, dict_[key])
