<?php
/**
 * Standalone Hit Counter API Endpoint
 *
 * Bootstraps WordPress to use wp_options for persistent counter storage.
 * Place in wp-content/plugins/onionpress-hit-counter/
 *
 * Usage:
 * GET  /wp-content/plugins/onionpress-hit-counter/standalone-counter.php?action=get
 * POST /wp-content/plugins/onionpress-hit-counter/standalone-counter.php?action=increment
 */

// Bootstrap WordPress
$wp_load = dirname(dirname(dirname(dirname(__FILE__)))) . '/wp-load.php';
if (!file_exists($wp_load)) {
    header('Content-Type: application/json');
    echo json_encode(array('success' => false, 'error' => 'WordPress not found'));
    exit;
}
require_once $wp_load;

$option_key = 'onionpress_hit_counter';

/**
 * Get current counter value
 */
function get_counter($option_key) {
    return (int) get_option($option_key, 0);
}

/**
 * Increment counter
 */
function increment_counter($option_key) {
    $count = get_counter($option_key) + 1;
    update_option($option_key, $count, 'no');
    return $count;
}

/**
 * Format counter with leading zeros
 */
function format_counter($count, $digits = 6) {
    return str_pad($count, $digits, '0', STR_PAD_LEFT);
}

// Handle API requests
header('Content-Type: application/json');

$action = isset($_GET['action']) ? $_GET['action'] : (isset($_POST['action']) ? $_POST['action'] : 'get');

if ($action === 'increment') {
    $new_count = increment_counter($option_key);
    echo json_encode(array(
        'success' => true,
        'count' => $new_count,
        'formatted' => format_counter($new_count)
    ));
} else {
    $count = get_counter($option_key);
    echo json_encode(array(
        'success' => true,
        'count' => $count,
        'formatted' => format_counter($count)
    ));
}
