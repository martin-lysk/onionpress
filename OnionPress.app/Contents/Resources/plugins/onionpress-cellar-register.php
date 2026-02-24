<?php
/**
 * Plugin Name: OnionPress Cellar Registration
 * Description: Accepts registration POSTs from OnionPress instances for OnionCellar failover.
 * Version: 2.0
 * Network: true
 */

// Safety check — must be loaded by WordPress
if (!defined('ABSPATH')) {
    exit;
}

// Load crypto helper and DB layer
require_once __DIR__ . '/onionpress-cellar-crypto.php';
require_once __DIR__ . '/onionpress-cellar-db.php';

/**
 * Intercept POST /register and POST /unregister early in the WordPress lifecycle.
 * This runs as an mu-plugin so it loads before themes and regular plugins.
 */
add_action('muplugins_loaded', 'onionpress_cellar_handle_register');
add_action('muplugins_loaded', 'onionpress_cellar_handle_unregister');
add_action('muplugins_loaded', 'onionpress_cellar_handle_online');
add_action('muplugins_loaded', 'onionpress_cellar_handle_offline');

function onionpress_cellar_handle_register() {
    // Only handle POST /register
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        return;
    }

    $request_uri = strtok($_SERVER['REQUEST_URI'], '?');
    if ($request_uri !== '/register') {
        return;
    }

    // Cellar must be unlocked to accept registrations
    if (!cellar_crypto_is_unlocked()) {
        onionpress_cellar_respond(503, ['error' => 'Cellar is locked', 'locked' => true]);
        return;
    }

    $master_key = cellar_crypto_read_unlocked_key();
    if ($master_key === false) {
        onionpress_cellar_respond(503, ['error' => 'Cellar is locked', 'locked' => true]);
        return;
    }

    // Read and validate JSON body
    $body = file_get_contents('php://input');
    $data = json_decode($body, true);

    if (!$data) {
        onionpress_cellar_respond(400, ['error' => 'Invalid JSON']);
        return;
    }

    $required = ['content_address', 'healthcheck_address', 'secret_key', 'public_key'];
    foreach ($required as $field) {
        if (empty($data[$field])) {
            onionpress_cellar_respond(400, ['error' => "Missing required field: $field"]);
            return;
        }
    }

    $content_address = $data['content_address'];
    $healthcheck_address = $data['healthcheck_address'];
    $secret_key_b64 = $data['secret_key'];
    $public_key_b64 = $data['public_key'];
    $version = isset($data['version']) ? $data['version'] : 'unknown';

    // Validate addresses look like .onion addresses
    if (!preg_match('/^[a-z2-7]{56}\.onion$/', $content_address)) {
        onionpress_cellar_respond(400, ['error' => 'Invalid content_address format']);
        return;
    }
    if (!preg_match('/^[a-z2-7]{56}\.onion$/', $healthcheck_address)) {
        onionpress_cellar_respond(400, ['error' => 'Invalid healthcheck_address format']);
        return;
    }

    // Validate base64-encoded keys
    $secret_key = base64_decode($secret_key_b64, true);
    $public_key = base64_decode($public_key_b64, true);
    if ($secret_key === false || $public_key === false) {
        onionpress_cellar_respond(400, ['error' => 'Invalid base64 key encoding']);
        return;
    }

    // Validate key sizes
    $sk_len = strlen($secret_key);
    if ($sk_len !== 64) {
        onionpress_cellar_respond(400, [
            'error' => "Invalid secret_key length: expected 64 bytes, got $sk_len",
        ]);
        return;
    }

    $pk_len = strlen($public_key);
    if ($pk_len === 64) {
        // 32-byte Tor header + 32-byte raw key — strip header
        $raw_pubkey = substr($public_key, 32);
    } elseif ($pk_len === 32) {
        // Raw 32-byte ed25519 public key
        $raw_pubkey = $public_key;
    } else {
        onionpress_cellar_respond(400, [
            'error' => "Invalid public_key length: expected 32 or 64 bytes, got $pk_len",
        ]);
        return;
    }

    // Verify content_address matches public_key (Tor v3 address derivation)
    if (in_array('sha3-256', hash_algos())) {
        $checksum_input = ".onion checksum" . $raw_pubkey . "\x03";
        $checksum = substr(hash('sha3-256', $checksum_input, true), 0, 2);
        $addr_bytes = $raw_pubkey . $checksum . "\x03";
        $derived_address = strtolower(onionpress_base32_encode($addr_bytes)) . '.onion';

        if ($derived_address !== $content_address) {
            onionpress_cellar_respond(400, [
                'error' => 'content_address does not match public_key',
                'expected' => $derived_address,
            ]);
            return;
        }
    }

    // Store Arti OpenSSH PEM key on disk (encrypted)
    $cellar_dir = '/var/lib/onionpress/cellar';
    $keys_dir = "$cellar_dir/keys/$content_address";

    if (!is_dir($keys_dir)) {
        mkdir($keys_dir, 0700, true);
    }

    // Decode and validate Arti PEM if provided, otherwise build from raw keys
    if (!empty($data['arti_key_pem'])) {
        $arti_pem = base64_decode($data['arti_key_pem'], true);
        if ($arti_pem === false || strpos($arti_pem, '-----BEGIN OPENSSH PRIVATE KEY-----') !== 0) {
            onionpress_cellar_respond(400, ['error' => 'Invalid arti_key_pem format']);
            return;
        }
    } else {
        // Legacy client without arti_key_pem — build PEM from raw keys
        $arti_pem = onionpress_build_openssh_pem($secret_key, $raw_pubkey);
        if ($arti_pem === false) {
            onionpress_cellar_respond(500, ['error' => 'Failed to build Arti PEM from raw keys']);
            return;
        }
    }

    // Encrypt and write the Arti PEM key file
    $enc_pem = cellar_crypto_encrypt_key($arti_pem, $master_key);

    if ($enc_pem === false) {
        onionpress_cellar_respond(500, ['error' => 'Encryption failed']);
        return;
    }

    file_put_contents("$keys_dir/ks_hs_id.ed25519_expanded_private.enc", $enc_pem);
    chmod("$keys_dir/ks_hs_id.ed25519_expanded_private.enc", 0600);

    // Remove old C-tor key files if present (migration cleanup)
    @unlink("$keys_dir/hs_ed25519_secret_key.enc");
    @unlink("$keys_dir/hs_ed25519_public_key.enc");
    @unlink("$keys_dir/hs_ed25519_secret_key");
    @unlink("$keys_dir/hs_ed25519_public_key");

    // Write hostname file (plaintext — public info)
    file_put_contents("$keys_dir/hostname", $content_address . "\n");
    chmod("$keys_dir/hostname", 0600);

    // Compute key_hash for future authentication (e.g. /unregister)
    $key_hash = hash('sha256', $secret_key);

    // Update registry (SQLite — concurrent-safe, no more JSON corruption)
    $db = cellar_db_connect();
    cellar_db_ensure_schema($db);

    // Check if entry already exists (by composite key)
    $existing = $db->prepare('SELECT 1 FROM registry WHERE content_address = ? AND healthcheck_address = ?');
    $existing->execute([$content_address, $healthcheck_address]);
    $found = (bool)$existing->fetchColumn();

    cellar_db_upsert_register($db, [
        'content_address' => $content_address,
        'healthcheck_address' => $healthcheck_address,
        'version' => $version,
        'key_hash' => $key_hash,
    ]);

    onionpress_cellar_respond(200, [
        'registered' => true,
        'content_address' => $content_address,
        'message' => $found ? 'Registration updated' : 'Registration created',
    ]);
}

/**
 * Handle POST /unregister — remove an entry from the cellar registry.
 *
 * Request body: {"content_address": "xxx.onion", "proof": "<sha256 hex>"}
 *
 * Local requests (127.0.0.1, ::1, Docker network 172.x) skip proof verification.
 * Remote requests must provide proof = sha256(secret_key_bytes) matching stored key_hash.
 */
function onionpress_cellar_handle_unregister() {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        return;
    }

    $request_uri = strtok($_SERVER['REQUEST_URI'], '?');
    if ($request_uri !== '/unregister') {
        return;
    }

    // Read and validate JSON body
    $body = file_get_contents('php://input');
    $data = json_decode($body, true);

    if (!$data) {
        onionpress_cellar_respond(400, ['error' => 'Invalid JSON']);
        return;
    }

    if (empty($data['content_address'])) {
        onionpress_cellar_respond(400, ['error' => 'Missing required field: content_address']);
        return;
    }

    $content_address = $data['content_address'];

    if (!preg_match('/^[a-z2-7]{56}\.onion$/', $content_address)) {
        onionpress_cellar_respond(400, ['error' => 'Invalid content_address format']);
        return;
    }

    // Look up entry in registry
    $db = cellar_db_connect();
    cellar_db_ensure_schema($db);
    $entry = cellar_db_get_entry($db, $content_address);

    if (!$entry) {
        onionpress_cellar_respond(404, ['error' => 'Entry not found']);
        return;
    }

    // Determine if this is a local request (skip proof)
    $remote_addr = $_SERVER['REMOTE_ADDR'] ?? '';
    $is_local = ($remote_addr === '127.0.0.1'
        || $remote_addr === '::1'
        || strpos($remote_addr, '172.') === 0);

    if (!$is_local) {
        // Remote request — require proof
        $proof = $data['proof'] ?? '';
        $stored_hash = $entry['key_hash'] ?? '';

        if ($stored_hash === '' || $stored_hash === null) {
            onionpress_cellar_respond(403, [
                'error' => 'No key_hash on file — re-register first to enable remote unregister',
            ]);
            return;
        }

        if ($proof === '' || !hash_equals($stored_hash, $proof)) {
            onionpress_cellar_respond(403, ['error' => 'Invalid proof']);
            return;
        }
    }

    $takeover_was_active = (bool)$entry['takeover_active'];

    // Remove key files from cellar storage
    $keys_dir = '/var/lib/onionpress/cellar/keys/' . $content_address;
    if (is_dir($keys_dir)) {
        $files = scandir($keys_dir);
        if ($files !== false) {
            foreach ($files as $f) {
                if ($f !== '.' && $f !== '..') {
                    unlink("$keys_dir/$f");
                }
            }
        }
        rmdir($keys_dir);
    }

    // Delete registry entry
    cellar_db_delete_entry($db, $content_address);

    // Note: if takeover was active, the caller must release the Arti config
    // in the tor container separately (this PHP code runs inside the WordPress
    // container and cannot exec into the tor container).
    onionpress_cellar_respond(200, [
        'unregistered' => true,
        'content_address' => $content_address,
        'takeover_was_active' => $takeover_was_active,
    ]);
}

/**
 * Handle POST /online — instance notifies cellar it is back online.
 *
 * Request body: {"content_address": "xxx.onion", "healthcheck_address": "yyy.onion", "proof": "<sha256 hex>"}
 *
 * Sets status='healthy', fail_count=0, fast_poll_remaining=20.
 * Does NOT require cellar to be unlocked — only updates DB state.
 * The poller detects takeover_active=1 + status='healthy' and triggers release.
 */
function onionpress_cellar_handle_online() {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        return;
    }

    $request_uri = strtok($_SERVER['REQUEST_URI'], '?');
    if ($request_uri !== '/online') {
        return;
    }

    $body = file_get_contents('php://input');
    $data = json_decode($body, true);

    if (!$data) {
        onionpress_cellar_respond(400, ['error' => 'Invalid JSON']);
        return;
    }

    if (empty($data['content_address'])) {
        onionpress_cellar_respond(400, ['error' => 'Missing required field: content_address']);
        return;
    }
    if (empty($data['healthcheck_address'])) {
        onionpress_cellar_respond(400, ['error' => 'Missing required field: healthcheck_address']);
        return;
    }

    $content_address = $data['content_address'];
    $healthcheck_address = $data['healthcheck_address'];

    if (!preg_match('/^[a-z2-7]{56}\.onion$/', $content_address)) {
        onionpress_cellar_respond(400, ['error' => 'Invalid content_address format']);
        return;
    }
    if (!preg_match('/^[a-z2-7]{56}\.onion$/', $healthcheck_address)) {
        onionpress_cellar_respond(400, ['error' => 'Invalid healthcheck_address format']);
        return;
    }

    $db = cellar_db_connect();
    cellar_db_ensure_schema($db);
    $entry = cellar_db_get_entry_by_pair($db, $content_address, $healthcheck_address);

    if (!$entry) {
        onionpress_cellar_respond(404, ['error' => 'Entry not found']);
        return;
    }

    // Auth check — same pattern as /unregister
    $remote_addr = $_SERVER['REMOTE_ADDR'] ?? '';
    $is_local = ($remote_addr === '127.0.0.1'
        || $remote_addr === '::1'
        || strpos($remote_addr, '172.') === 0);

    if (!$is_local) {
        $proof = $data['proof'] ?? '';
        $stored_hash = $entry['key_hash'] ?? '';

        if ($stored_hash === '' || $stored_hash === null) {
            onionpress_cellar_respond(403, [
                'error' => 'No key_hash on file — re-register first',
            ]);
            return;
        }

        if ($proof === '' || !hash_equals($stored_hash, $proof)) {
            onionpress_cellar_respond(403, ['error' => 'Invalid proof']);
            return;
        }
    }

    $takeover_was_active = (bool)$entry['takeover_active'];
    $now = gmdate('Y-m-d\TH:i:s\Z');

    cellar_db_update_entry($db, $content_address, $healthcheck_address, [
        'status' => 'healthy',
        'fail_count' => 0,
        'fast_poll_remaining' => 20,
        'last_contact' => $now,
    ]);

    onionpress_cellar_respond(200, [
        'online' => true,
        'content_address' => $content_address,
        'takeover_was_active' => $takeover_was_active,
    ]);
}

/**
 * Handle POST /offline — instance notifies cellar it is going offline.
 *
 * Request body: {"content_address": "xxx.onion", "healthcheck_address": "yyy.onion", "proof": "<sha256 hex>"}
 *
 * Sets status='failing', fail_count=10 (== FAIL_THRESHOLD in cellar-poller.py),
 * fast_poll_remaining=20. The poller sees fail_count >= threshold on next pass
 * and triggers takeover immediately.
 * Does NOT require cellar to be unlocked — only updates DB state.
 */
function onionpress_cellar_handle_offline() {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        return;
    }

    $request_uri = strtok($_SERVER['REQUEST_URI'], '?');
    if ($request_uri !== '/offline') {
        return;
    }

    $body = file_get_contents('php://input');
    $data = json_decode($body, true);

    if (!$data) {
        onionpress_cellar_respond(400, ['error' => 'Invalid JSON']);
        return;
    }

    if (empty($data['content_address'])) {
        onionpress_cellar_respond(400, ['error' => 'Missing required field: content_address']);
        return;
    }
    if (empty($data['healthcheck_address'])) {
        onionpress_cellar_respond(400, ['error' => 'Missing required field: healthcheck_address']);
        return;
    }

    $content_address = $data['content_address'];
    $healthcheck_address = $data['healthcheck_address'];

    if (!preg_match('/^[a-z2-7]{56}\.onion$/', $content_address)) {
        onionpress_cellar_respond(400, ['error' => 'Invalid content_address format']);
        return;
    }
    if (!preg_match('/^[a-z2-7]{56}\.onion$/', $healthcheck_address)) {
        onionpress_cellar_respond(400, ['error' => 'Invalid healthcheck_address format']);
        return;
    }

    $db = cellar_db_connect();
    cellar_db_ensure_schema($db);
    $entry = cellar_db_get_entry_by_pair($db, $content_address, $healthcheck_address);

    if (!$entry) {
        onionpress_cellar_respond(404, ['error' => 'Entry not found']);
        return;
    }

    // Auth check — same pattern as /unregister
    $remote_addr = $_SERVER['REMOTE_ADDR'] ?? '';
    $is_local = ($remote_addr === '127.0.0.1'
        || $remote_addr === '::1'
        || strpos($remote_addr, '172.') === 0);

    if (!$is_local) {
        $proof = $data['proof'] ?? '';
        $stored_hash = $entry['key_hash'] ?? '';

        if ($stored_hash === '' || $stored_hash === null) {
            onionpress_cellar_respond(403, [
                'error' => 'No key_hash on file — re-register first',
            ]);
            return;
        }

        if ($proof === '' || !hash_equals($stored_hash, $proof)) {
            onionpress_cellar_respond(403, ['error' => 'Invalid proof']);
            return;
        }
    }

    $takeover_was_active = (bool)$entry['takeover_active'];
    $now = gmdate('Y-m-d\TH:i:s\Z');

    // fail_count=10 matches FAIL_THRESHOLD in cellar-poller.py
    cellar_db_update_entry($db, $content_address, $healthcheck_address, [
        'status' => 'failing',
        'fail_count' => 10,
        'fast_poll_remaining' => 20,
        'last_contact' => $now,
    ]);

    onionpress_cellar_respond(200, [
        'offline' => true,
        'content_address' => $content_address,
        'takeover_active' => $takeover_was_active,
    ]);
}

/**
 * Build an OpenSSH PEM private key for Arti from raw Ed25519 keys.
 *
 * Produces the same format as key_manager.build_openssh_key():
 *   openssh-key-v1 with key type "ed25519-expanded@spec.torproject.org"
 *
 * @param string $private_key  64-byte expanded Ed25519 private key
 * @param string $public_key   32-byte Ed25519 public key
 * @return string|false  PEM-encoded OpenSSH private key, or false on error
 */
function onionpress_build_openssh_pem($private_key, $public_key) {
    if (strlen($private_key) !== 64 || strlen($public_key) !== 32) {
        return false;
    }

    $key_type = 'ed25519-expanded@spec.torproject.org';

    // pack_string: uint32 big-endian length prefix + data
    $ps = function($data) {
        return pack('N', strlen($data)) . $data;
    };

    // Public key blob
    $pub_blob = $ps($key_type) . $ps($public_key);

    // Check integers (random, must match)
    $check = random_bytes(4);

    // Private key blob (unencrypted)
    $priv_inner = $check . $check
        . $ps($key_type)
        . $ps($public_key)
        . $ps($private_key)
        . $ps('');  // empty comment

    // Pad to 8-byte alignment
    $pad_len = (8 - (strlen($priv_inner) % 8)) % 8;
    for ($i = 1; $i <= $pad_len; $i++) {
        $priv_inner .= chr($i);
    }

    // Assemble the full binary blob
    $blob = "openssh-key-v1\x00"   // magic
        . $ps('none')              // cipher
        . $ps('none')              // kdf
        . $ps('')                  // kdf options
        . pack('N', 1)             // num keys
        . $ps($pub_blob)           // public key section
        . $ps($priv_inner);        // private key section

    return "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        . chunk_split(base64_encode($blob), 70, "\n")
        . "-----END OPENSSH PRIVATE KEY-----\n";
}

/**
 * RFC 4648 base32 encode (no padding). PHP has no built-in base32.
 */
function onionpress_base32_encode($data) {
    $alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
    $binary = '';
    for ($i = 0; $i < strlen($data); $i++) {
        $binary .= str_pad(decbin(ord($data[$i])), 8, '0', STR_PAD_LEFT);
    }
    $result = '';
    for ($i = 0; $i + 5 <= strlen($binary); $i += 5) {
        $result .= $alphabet[bindec(substr($binary, $i, 5))];
    }
    return $result;
}

/**
 * Send a JSON response and exit.
 */
function onionpress_cellar_respond($status_code, $data) {
    http_response_code($status_code);
    header('Content-Type: application/json');
    echo json_encode($data);
    exit;
}

// ---------------------------------------------------------------------------
// WordPress hooks for cellar unlock / key slot management
// ---------------------------------------------------------------------------

/**
 * Unlock the cellar on WordPress admin login.
 *
 * On standard form login, $_POST['pwd'] contains the plaintext password.
 * - If no master-key.json: generate master key, create first slot, write unlocked file
 * - If user has slot: derive key, decrypt master key, write unlocked file
 * - If user has no slot but cellar already unlocked: lazy enrollment
 * - After unlock: migrate any remaining plaintext keys to encrypted
 */
add_action('wp_login', 'onionpress_cellar_unlock', 10, 2);

function onionpress_cellar_unlock($user_login, $user) {
    // Only for super admins (multisite network admins)
    if (!$user->has_cap('manage_network')) {
        return;
    }

    // Need the plaintext password from the login form
    $password = isset($_POST['pwd']) ? $_POST['pwd'] : null;
    if ($password === null || $password === '') {
        return;
    }

    $slot_data = cellar_crypto_load_slots();
    $slots = &$slot_data['slots'];

    if (empty($slots)) {
        // First admin login ever — generate master key and create first slot
        $master_key = cellar_crypto_generate_master_key();
        $salt = random_bytes(16);
        $derived = cellar_crypto_derive_key($password, $salt);
        $encrypted = cellar_crypto_encrypt_slot($master_key, $derived);

        if ($encrypted === false) {
            return;
        }

        $slots[$user_login] = array_merge($encrypted, [
            'salt' => base64_encode($salt),
            'created_at' => gmdate('Y-m-d\TH:i:s\Z'),
        ]);

        cellar_crypto_save_slots($slot_data);
        cellar_crypto_write_unlocked_key($master_key);
        onionpress_cellar_migrate_plaintext_keys($master_key);
        return;
    }

    if (isset($slots[$user_login])) {
        // User has a slot — try to decrypt the master key
        $slot = $slots[$user_login];
        $salt = base64_decode($slot['salt'], true);
        if ($salt === false) {
            return;
        }

        $derived = cellar_crypto_derive_key($password, $salt);
        $master_key = cellar_crypto_decrypt_slot($slot, $derived);

        if ($master_key === false) {
            return;
        }

        cellar_crypto_write_unlocked_key($master_key);
        onionpress_cellar_migrate_plaintext_keys($master_key);
        return;
    }

    // User has no slot — lazy enrollment if cellar is already unlocked
    $master_key = cellar_crypto_read_unlocked_key();
    if ($master_key === false) {
        return;
    }

    $salt = random_bytes(16);
    $derived = cellar_crypto_derive_key($password, $salt);
    $encrypted = cellar_crypto_encrypt_slot($master_key, $derived);

    if ($encrypted === false) {
        return;
    }

    $slots[$user_login] = array_merge($encrypted, [
        'salt' => base64_encode($salt),
        'created_at' => gmdate('Y-m-d\TH:i:s\Z'),
    ]);

    cellar_crypto_save_slots($slot_data);
}

/**
 * Re-encrypt a user's key slot when their password changes.
 */
add_action('profile_update', 'onionpress_cellar_password_changed', 10, 2);

function onionpress_cellar_password_changed($user_id, $old_user_data) {
    // Only process if a new password was submitted
    $new_password = isset($_POST['pass1']) ? $_POST['pass1'] : null;
    if ($new_password === null || $new_password === '') {
        return;
    }

    // Only for super admins
    $user = get_userdata($user_id);
    if (!$user || !$user->has_cap('manage_network')) {
        return;
    }

    // Cellar must be unlocked to re-encrypt the slot
    $master_key = cellar_crypto_read_unlocked_key();
    if ($master_key === false) {
        return;
    }

    $slot_data = cellar_crypto_load_slots();
    $slots = &$slot_data['slots'];

    $salt = random_bytes(16);
    $derived = cellar_crypto_derive_key($new_password, $salt);
    $encrypted = cellar_crypto_encrypt_slot($master_key, $derived);

    if ($encrypted === false) {
        return;
    }

    $slots[$user->user_login] = array_merge($encrypted, [
        'salt' => base64_encode($salt),
        'created_at' => gmdate('Y-m-d\TH:i:s\Z'),
    ]);

    cellar_crypto_save_slots($slot_data);
}

/**
 * Remove a user's key slot when their super admin privileges are revoked.
 */
add_action('revoke_super_admin', 'onionpress_cellar_super_admin_revoked');

function onionpress_cellar_super_admin_revoked($user_id) {
    $user = get_userdata($user_id);
    if (!$user) {
        return;
    }

    $slot_data = cellar_crypto_load_slots();
    if (isset($slot_data['slots'][$user->user_login])) {
        unset($slot_data['slots'][$user->user_login]);
        cellar_crypto_save_slots($slot_data);
    }
}

/**
 * Migrate plaintext Arti key files to encrypted format.
 * Scans cellar/keys/ subdirectories for plaintext PEM files without .enc companions,
 * encrypts them, and removes the plaintext originals.
 * Also cleans up any remaining C-tor key files from the pre-Arti era.
 */
function onionpress_cellar_migrate_plaintext_keys($master_key) {
    $keys_base = '/var/lib/onionpress/cellar/keys';
    if (!is_dir($keys_base)) {
        return;
    }

    $dirs = scandir($keys_base);
    if ($dirs === false) {
        return;
    }

    foreach ($dirs as $dir) {
        if ($dir === '.' || $dir === '..') {
            continue;
        }

        $dir_path = "$keys_base/$dir";
        if (!is_dir($dir_path)) {
            continue;
        }

        // Migrate plaintext Arti PEM to encrypted
        $pem_plain = "$dir_path/ks_hs_id.ed25519_expanded_private";
        $pem_enc = "$dir_path/ks_hs_id.ed25519_expanded_private.enc";

        if (file_exists($pem_plain) && !file_exists($pem_enc)) {
            $pem_data = file_get_contents($pem_plain);
            if ($pem_data !== false) {
                $encrypted = cellar_crypto_encrypt_key($pem_data, $master_key);
                if ($encrypted !== false) {
                    file_put_contents($pem_enc, $encrypted);
                    chmod($pem_enc, 0600);
                    unlink($pem_plain);
                }
            }
        }

        // Clean up old C-tor key files (no longer used)
        @unlink("$dir_path/hs_ed25519_secret_key");
        @unlink("$dir_path/hs_ed25519_secret_key.enc");
        @unlink("$dir_path/hs_ed25519_public_key");
        @unlink("$dir_path/hs_ed25519_public_key.enc");
    }
}
