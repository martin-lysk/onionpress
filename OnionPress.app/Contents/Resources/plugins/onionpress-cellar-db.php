<?php
/**
 * OnionPress Cellar — SQLite Database Access Layer
 *
 * Central DB module for the cellar registry. Both the PHP registration
 * endpoint and the Python poller access SQLite through this file:
 *   - PHP: require_once and call functions directly
 *   - Python: `docker exec onionpress-wordpress php /path/to/onionpress-cellar-db.php <command>`
 *
 * WAL journaling + busy_timeout handles concurrent access from
 * registration and polling without the corruption risk of JSON files.
 *
 * Schema: composite primary key (content_address, healthcheck_address) allows
 * multiple instances to register the same .onion address from different machines.
 */

define('CELLAR_DB_DIR', '/var/lib/onionpress/cellar');
define('CELLAR_DB_PATH', CELLAR_DB_DIR . '/registry.db');
define('CELLAR_JSON_PATH', CELLAR_DB_DIR . '/registry.json');

/**
 * Open (or create) the SQLite database with WAL mode and busy timeout.
 */
function cellar_db_connect() {
    if (!is_dir(CELLAR_DB_DIR)) {
        mkdir(CELLAR_DB_DIR, 0750, true);
    }
    $db = new PDO('sqlite:' . CELLAR_DB_PATH);
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    $db->exec('PRAGMA journal_mode=WAL');
    $db->exec('PRAGMA busy_timeout=5000');
    return $db;
}

/**
 * Create the registry table if it doesn't exist, and migrate old schema.
 *
 * New schema uses composite PK (content_address, healthcheck_address),
 * renames last_healthcheck → last_contact, adds last_redirect.
 */
function cellar_db_ensure_schema($db) {
    // Check if we need to migrate from old single-PK schema
    $cols = [];
    try {
        $cols = $db->query('PRAGMA table_info(registry)')->fetchAll(PDO::FETCH_ASSOC);
    } catch (Exception $e) {
        // Table doesn't exist yet — will be created below
    }

    $col_names = array_column($cols, 'name');
    $needs_migration = false;

    if (!empty($cols)) {
        // Table exists — check if it uses old schema (single PK on content_address)
        // Old schema has last_healthcheck; new schema has last_contact
        if (in_array('last_healthcheck', $col_names) && !in_array('last_contact', $col_names)) {
            $needs_migration = true;
        }
        // Also check: old schema has content_address as sole PK (pk=1 and only one pk column)
        $pk_cols = array_filter($cols, function($c) { return $c['pk'] > 0; });
        if (count($pk_cols) === 1) {
            $needs_migration = true;
        }
    }

    if ($needs_migration) {
        // Migrate: create new table, copy data, swap
        $db->exec('CREATE TABLE IF NOT EXISTS registry_new (
            content_address     TEXT NOT NULL,
            healthcheck_address TEXT NOT NULL,
            registered_at       TEXT NOT NULL,
            version             TEXT NOT NULL DEFAULT \'unknown\',
            status              TEXT NOT NULL DEFAULT \'healthy\',
            last_contact        TEXT,
            last_redirect       TEXT,
            fail_count          INTEGER NOT NULL DEFAULT 0,
            takeover_active     INTEGER NOT NULL DEFAULT 0,
            fast_poll_remaining INTEGER NOT NULL DEFAULT 0,
            key_hash            TEXT,
            PRIMARY KEY (content_address, healthcheck_address)
        )');

        // Copy data — map last_healthcheck → last_contact
        $old_cols_str = in_array('key_hash', $col_names)
            ? 'content_address, healthcheck_address, registered_at, version, status, last_healthcheck, fail_count, takeover_active, fast_poll_remaining, key_hash'
            : 'content_address, healthcheck_address, registered_at, version, status, last_healthcheck, fail_count, takeover_active, fast_poll_remaining, NULL';
        $new_cols_str = 'content_address, healthcheck_address, registered_at, version, status, last_contact, fail_count, takeover_active, fast_poll_remaining, key_hash';

        $db->exec("INSERT OR IGNORE INTO registry_new ($new_cols_str)
                    SELECT $old_cols_str FROM registry");
        $db->exec('DROP TABLE registry');
        $db->exec('ALTER TABLE registry_new RENAME TO registry');
        return;
    }

    // Fresh install or already migrated — create with new schema
    $db->exec('CREATE TABLE IF NOT EXISTS registry (
        content_address     TEXT NOT NULL,
        healthcheck_address TEXT NOT NULL,
        registered_at       TEXT NOT NULL,
        version             TEXT NOT NULL DEFAULT \'unknown\',
        status              TEXT NOT NULL DEFAULT \'healthy\',
        last_contact        TEXT,
        last_redirect       TEXT,
        fail_count          INTEGER NOT NULL DEFAULT 0,
        takeover_active     INTEGER NOT NULL DEFAULT 0,
        fast_poll_remaining INTEGER NOT NULL DEFAULT 0,
        key_hash            TEXT,
        PRIMARY KEY (content_address, healthcheck_address)
    )');

    // Migration: add columns that may be missing on older new-schema tables
    if (!empty($col_names)) {
        if (!in_array('key_hash', $col_names)) {
            $db->exec('ALTER TABLE registry ADD COLUMN key_hash TEXT');
        }
        if (!in_array('last_redirect', $col_names)) {
            $db->exec('ALTER TABLE registry ADD COLUMN last_redirect TEXT');
        }
    }
}

/**
 * Import entries from registry.json if it exists, then rename to .migrated.
 * Uses INSERT OR IGNORE so existing DB rows are not overwritten.
 */
function cellar_db_migrate_json($db) {
    $json_path = CELLAR_JSON_PATH;
    $migrated_path = $json_path . '.migrated';

    // Already migrated or no JSON file
    if (!file_exists($json_path) || file_exists($migrated_path)) {
        return 0;
    }

    $data = json_decode(file_get_contents($json_path), true);
    if (!is_array($data) || empty($data)) {
        // Empty or corrupt JSON — just rename it
        rename($json_path, $migrated_path);
        return 0;
    }

    $stmt = $db->prepare('INSERT OR IGNORE INTO registry
        (content_address, healthcheck_address, registered_at, version,
         status, last_contact, fail_count, takeover_active, fast_poll_remaining)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)');

    $count = 0;
    $db->beginTransaction();
    foreach ($data as $entry) {
        $stmt->execute([
            $entry['content_address'] ?? '',
            $entry['healthcheck_address'] ?? '',
            $entry['registered_at'] ?? gmdate('Y-m-d\TH:i:s\Z'),
            $entry['version'] ?? 'unknown',
            $entry['status'] ?? 'healthy',
            $entry['last_healthcheck'] ?? $entry['last_contact'] ?? null,
            (int)($entry['fail_count'] ?? 0),
            (int)(!empty($entry['takeover_active']) ? 1 : 0),
            (int)($entry['_fast_poll_remaining'] ?? $entry['fast_poll_remaining'] ?? 0),
        ]);
        $count++;
    }
    $db->commit();

    rename($json_path, $migrated_path);
    return $count;
}

/**
 * Return all registry rows as an array of associative arrays.
 */
function cellar_db_read_all($db) {
    $stmt = $db->query('SELECT * FROM registry ORDER BY registered_at');
    $rows = $stmt->fetchAll(PDO::FETCH_ASSOC);
    // Cast integer columns
    foreach ($rows as &$row) {
        $row['fail_count'] = (int)$row['fail_count'];
        $row['takeover_active'] = (int)$row['takeover_active'];
        $row['fast_poll_remaining'] = (int)$row['fast_poll_remaining'];
    }
    return $rows;
}

/**
 * Insert or update a registration (called from the /register endpoint).
 * On re-registration: resets fail_count and status to healthy.
 * If the entry was taken over, the poller detects takeover_active=1 + status='healthy'
 * and does an immediate release. Registration over Tor proves the instance is alive.
 *
 * Upserts on (content_address, healthcheck_address) — same instance re-registering
 * updates in place; a new instance with different healthcheck gets a new row.
 */
function cellar_db_upsert_register($db, $data) {
    $now = gmdate('Y-m-d\TH:i:s\Z');
    $stmt = $db->prepare('INSERT INTO registry
        (content_address, healthcheck_address, registered_at, version, key_hash, last_contact)
        VALUES (:ca, :ha, :ra, :ver, :kh, :lc)
        ON CONFLICT(content_address, healthcheck_address) DO UPDATE SET
            registered_at = :ra,
            version = :ver,
            key_hash = :kh,
            fail_count = 0,
            status = \'healthy\',
            fast_poll_remaining = 0,
            last_contact = :lc');
    $stmt->execute([
        ':ca'  => $data['content_address'],
        ':ha'  => $data['healthcheck_address'],
        ':ra'  => $now,
        ':ver' => $data['version'] ?? 'unknown',
        ':kh'  => $data['key_hash'] ?? null,
        ':lc'  => $now,
    ]);
    // Return whether this was an insert or update
    return $db->query("SELECT changes()")->fetchColumn() > 0;
}

/**
 * Batch-update poll fields for multiple entries in a single transaction.
 * Input: array of entries with content_address + healthcheck_address + poll fields.
 */
function cellar_db_batch_update_poll($db, $entries) {
    $stmt = $db->prepare('UPDATE registry SET
        status = :status,
        last_contact = :lc,
        fail_count = :fc,
        takeover_active = :ta,
        fast_poll_remaining = :fpr
        WHERE content_address = :ca AND healthcheck_address = :ha');

    $db->beginTransaction();
    $updated = 0;
    foreach ($entries as $entry) {
        $stmt->execute([
            ':status' => $entry['status'] ?? 'healthy',
            ':lc'     => $entry['last_contact'] ?? null,
            ':fc'     => (int)($entry['fail_count'] ?? 0),
            ':ta'     => (int)(!empty($entry['takeover_active']) ? 1 : 0),
            ':fpr'    => (int)($entry['fast_poll_remaining'] ?? 0),
            ':ca'     => $entry['content_address'],
            ':ha'     => $entry['healthcheck_address'],
        ]);
        $updated++;
    }
    $db->commit();
    return $updated;
}

/**
 * Look up a single registry entry by content_address (returns first match).
 */
function cellar_db_get_entry($db, $content_address) {
    $stmt = $db->prepare('SELECT * FROM registry WHERE content_address = ? LIMIT 1');
    $stmt->execute([$content_address]);
    $row = $stmt->fetch(PDO::FETCH_ASSOC);
    if ($row) {
        $row['fail_count'] = (int)$row['fail_count'];
        $row['takeover_active'] = (int)$row['takeover_active'];
        $row['fast_poll_remaining'] = (int)$row['fast_poll_remaining'];
    }
    return $row ?: null;
}

/**
 * Look up a specific registry entry by (content_address, healthcheck_address).
 */
function cellar_db_get_entry_by_pair($db, $content_address, $healthcheck_address) {
    $stmt = $db->prepare('SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?');
    $stmt->execute([$content_address, $healthcheck_address]);
    $row = $stmt->fetch(PDO::FETCH_ASSOC);
    if ($row) {
        $row['fail_count'] = (int)$row['fail_count'];
        $row['takeover_active'] = (int)$row['takeover_active'];
        $row['fast_poll_remaining'] = (int)$row['fast_poll_remaining'];
    }
    return $row ?: null;
}

/**
 * Delete entry/entries by content_address, optionally filtered by healthcheck_address.
 * If $healthcheck_address is provided, deletes only that specific row.
 * Otherwise, deletes all rows for the content_address.
 */
function cellar_db_delete_entry($db, $content_address, $healthcheck_address = null) {
    if ($healthcheck_address !== null) {
        $stmt = $db->prepare('DELETE FROM registry WHERE content_address = ? AND healthcheck_address = ?');
        $stmt->execute([$content_address, $healthcheck_address]);
    } else {
        $stmt = $db->prepare('DELETE FROM registry WHERE content_address = ?');
        $stmt->execute([$content_address]);
    }
    return $db->query("SELECT changes()")->fetchColumn() > 0;
}

/**
 * Update specific fields on a single row by (content_address, healthcheck_address).
 * Only whitelisted columns may be updated.
 */
function cellar_db_update_entry($db, $content_address, $healthcheck_address, $fields) {
    $allowed = ['status', 'fail_count', 'last_contact', 'last_redirect',
                'takeover_active', 'fast_poll_remaining', 'key_hash', 'version'];
    $sets = [];
    $params = [];
    foreach ($fields as $col => $val) {
        if (in_array($col, $allowed)) {
            $sets[] = "$col = ?";
            $params[] = $val;
        }
    }
    if (empty($sets)) {
        return false;
    }
    $params[] = $content_address;
    $params[] = $healthcheck_address;
    $sql = 'UPDATE registry SET ' . implode(', ', $sets)
         . ' WHERE content_address = ? AND healthcheck_address = ?';
    $stmt = $db->prepare($sql);
    $stmt->execute($params);
    return $db->query("SELECT changes()")->fetchColumn() > 0;
}

/**
 * Update specific fields on ALL rows matching a content_address.
 * Used for last_redirect (applies to all instances of the same .onion).
 */
function cellar_db_update_by_content($db, $content_address, $fields) {
    $allowed = ['status', 'fail_count', 'last_contact', 'last_redirect',
                'takeover_active', 'fast_poll_remaining'];
    $sets = [];
    $params = [];
    foreach ($fields as $col => $val) {
        if (in_array($col, $allowed)) {
            $sets[] = "$col = ?";
            $params[] = $val;
        }
    }
    if (empty($sets)) {
        return 0;
    }
    $params[] = $content_address;
    $sql = 'UPDATE registry SET ' . implode(', ', $sets) . ' WHERE content_address = ?';
    $stmt = $db->prepare($sql);
    $stmt->execute($params);
    return (int)$db->query("SELECT changes()")->fetchColumn();
}

/**
 * Delete entries matching a specific version string.
 */
function cellar_db_delete_by_version($db, $version) {
    $stmt = $db->prepare('DELETE FROM registry WHERE version = ?');
    $stmt->execute([$version]);
    return $db->query("SELECT changes()")->fetchColumn();
}

/**
 * Count rows, optionally with a WHERE clause.
 */
function cellar_db_count($db, $where = '') {
    $sql = 'SELECT COUNT(*) FROM registry';
    if ($where !== '') {
        $sql .= ' ' . $where;
    }
    return (int)$db->query($sql)->fetchColumn();
}

/**
 * Return content_addresses matching a WHERE clause.
 */
function cellar_db_query_addresses($db, $where = '') {
    $sql = 'SELECT content_address FROM registry';
    if ($where !== '') {
        $sql .= ' ' . $where;
    }
    return $db->query($sql)->fetchAll(PDO::FETCH_COLUMN);
}

// ---------------------------------------------------------------------------
// CLI interface — when run directly: php onionpress-cellar-db.php <command>
// ---------------------------------------------------------------------------
if (php_sapi_name() === 'cli' && isset($argv[0]) && realpath($argv[0]) === realpath(__FILE__)) {
    $command = $argv[1] ?? '';

    try {
        $db = cellar_db_connect();
        cellar_db_ensure_schema($db);

        switch ($command) {
            case 'init':
                $migrated = cellar_db_migrate_json($db);
                echo json_encode(['ok' => true, 'migrated' => $migrated]) . "\n";
                break;

            case 'read-all':
                echo json_encode(cellar_db_read_all($db)) . "\n";
                break;

            case 'batch-upsert-poll':
                $input = file_get_contents('php://stdin');
                $entries = json_decode($input, true);
                if (!is_array($entries)) {
                    fwrite(STDERR, "Invalid JSON on stdin\n");
                    exit(1);
                }
                $updated = cellar_db_batch_update_poll($db, $entries);
                echo json_encode(['ok' => true, 'updated' => $updated]) . "\n";
                break;

            case 'delete-entry':
                $addr = $argv[2] ?? '';
                if ($addr === '') {
                    fwrite(STDERR, "Usage: delete-entry <content_address> [healthcheck_address]\n");
                    exit(1);
                }
                $ha = $argv[3] ?? null;
                $deleted = cellar_db_delete_entry($db, $addr, $ha !== '' ? $ha : null);
                echo json_encode(['ok' => true, 'deleted' => $deleted]) . "\n";
                break;

            case 'delete-by-version':
                $version = $argv[2] ?? '';
                if ($version === '') {
                    fwrite(STDERR, "Usage: delete-by-version <version>\n");
                    exit(1);
                }
                $deleted = cellar_db_delete_by_version($db, $version);
                echo json_encode(['ok' => true, 'deleted' => $deleted]) . "\n";
                break;

            case 'count':
                $where = $argv[2] ?? '';
                echo cellar_db_count($db, $where) . "\n";
                break;

            case 'query-addresses':
                $where = $argv[2] ?? '';
                $addrs = cellar_db_query_addresses($db, $where);
                echo implode("\n", $addrs) . "\n";
                break;

            default:
                fwrite(STDERR, "Usage: php onionpress-cellar-db.php <command>\n");
                fwrite(STDERR, "Commands: init, read-all, batch-upsert-poll, delete-entry, delete-by-version, count, query-addresses\n");
                exit(1);
        }
    } catch (Exception $e) {
        fwrite(STDERR, "Error: " . $e->getMessage() . "\n");
        exit(1);
    }
}
