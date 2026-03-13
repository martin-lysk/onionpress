<?php
/**
 * Plugin Name: OnionPress Wayback Archive
 * Description: Automatically archives published posts and the homepage to the
 *              Internet Archive Wayback Machine.
 * Version:     1.3
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// OnionPress version — read once from the shared volume, cached per request.
function onionpress_version() {
    static $ver = null;
    if ( $ver === null ) {
        $f = '/var/lib/onionpress/version';
        $ver = file_exists( $f ) ? trim( file_get_contents( $f ) ) : 'unknown';
    }
    return $ver;
}

/**
 * Get the archive.org S3 API authorization header, if credentials are configured.
 *
 * Returns "LOW access:secret" string or empty string if not configured.
 * Reads from wp_options (set during OnionPress setup).
 */
function onionpress_wayback_auth_header() {
    static $header = null;
    if ( $header !== null ) {
        return $header;
    }

    // Use main site options (blog 1) for network-wide credentials
    $access = get_blog_option( 1, 'onionpress_archive_s3_access', '' );
    $secret = get_blog_option( 1, 'onionpress_archive_s3_secret', '' );

    if ( $access && $secret ) {
        $header = 'LOW ' . $access . ':' . $secret;
    } else {
        $header = '';
    }

    return $header;
}


/**
 * Archive to the Wayback Machine when a post or page is published/updated.
 */
add_action( 'save_post', function ( $post_id, $post, $update ) {
    // Skip autosaves and revisions
    if ( defined( 'DOING_AUTOSAVE' ) && DOING_AUTOSAVE ) {
        return;
    }
    if ( wp_is_post_revision( $post_id ) ) {
        return;
    }

    // Only archive published posts and pages
    if ( $post->post_status !== 'publish' ) {
        return;
    }
    if ( ! in_array( $post->post_type, array( 'post', 'page' ), true ) ) {
        return;
    }

    // Read the .onion address from the shared volume
    $onion_file = '/var/lib/onionpress/onion_address';
    if ( ! file_exists( $onion_file ) ) {
        // Tor not ready — queue the post path for later (the menubar will
        // prepend the .onion address when it drains the queue)
        $permalink = get_permalink( $post_id );
        $path      = wp_parse_url( $permalink, PHP_URL_PATH ) ?: '/';
        onionpress_wayback_queue_path( $path );
        return;
    }
    $onion_addr = trim( file_get_contents( $onion_file ) );
    if ( empty( $onion_addr ) ) {
        $permalink = get_permalink( $post_id );
        $path      = wp_parse_url( $permalink, PHP_URL_PATH ) ?: '/';
        onionpress_wayback_queue_path( $path );
        return;
    }

    // Get the post path from the permalink (strip the scheme+host)
    $permalink = get_permalink( $post_id );
    $path      = wp_parse_url( $permalink, PHP_URL_PATH ) ?: '/';

    // Build URLs to archive
    $urls = array();

    // 1. Post .onion URL
    $urls[] = 'http://' . $onion_addr . $path;

    // 2. Homepage .onion URL
    $urls[] = 'http://' . $onion_addr . '/';

    // 3. RSS feed .onion URL
    $urls[] = 'http://' . $onion_addr . '/feed/';


    // Deduplicate (e.g. if the post IS the homepage)
    $urls = array_unique( $urls );

    // Try clearnet first (faster, no Tor overhead), fall back to .onion via Tor.
    // The clearnet endpoint works because the WordPress container has internet
    // access through Colima's NAT.
    $endpoints = array(
        array(
            'url'   => 'https://web.archive.org/save',
            'proxy' => null,
        ),
        array(
            'url'   => 'http://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save',
            'proxy' => 'socks5h://onionpress-tor:9050',
        ),
    );

    $auth = onionpress_wayback_auth_header();

    $failed_urls = array();
    $first = true;
    foreach ( $urls as $url ) {
        // Small delay between requests to avoid SPN rate limits (429)
        if ( ! $first ) {
            sleep( 2 );
        }
        $first = false;

        if ( ! onionpress_wayback_submit( $endpoints, $url, $auth ) ) {
            $failed_urls[] = $url;
        }
    }

    // Queue any URLs that failed all endpoints
    if ( ! empty( $failed_urls ) ) {
        onionpress_wayback_queue_urls( $failed_urls );
    }
}, 10, 3 );

/**
 * Submit a URL to the Wayback Machine Save Page Now API.
 *
 * Uses PHP curl directly (not wp_remote_post) because WordPress HTTP API
 * does not support SOCKS5 proxies.
 *
 * Tries each endpoint in order; stops on first success.
 * Returns true on success, false if all endpoints failed.
 */
function onionpress_wayback_submit( $endpoints, $url, $auth = '' ) {
    if ( ! function_exists( 'curl_init' ) ) {
        error_log( '[OnionPress Wayback] curl extension not available' );
        return false;
    }

    $user_agent = 'OnionPress/' . onionpress_version() . ' (+https://github.com/brewsterkahle/onionpress)';

    foreach ( $endpoints as $ep ) {
        error_log( '[OnionPress Wayback] Archiving: ' . $url . ' via ' . $ep['url'] . ( $auth ? ' (authenticated)' : ' (no auth)' ) );

        $headers = array( 'Accept: application/json' );
        if ( $auth ) {
            $headers[] = 'Authorization: ' . $auth;
        }

        $ch = curl_init();
        $opts = array(
            CURLOPT_URL            => $ep['url'],
            CURLOPT_POST           => true,
            CURLOPT_POSTFIELDS     => http_build_query( array( 'url' => $url ) ),
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => 30,
            CURLOPT_CONNECTTIMEOUT => 15,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_MAXREDIRS      => 3,
            CURLOPT_USERAGENT      => $user_agent,
            CURLOPT_HTTPHEADER     => $headers,
            // .onion HTTPS uses self-signed certs; safe because Tor provides
            // end-to-end encryption already.
            CURLOPT_SSL_VERIFYPEER => false,
            CURLOPT_SSL_VERIFYHOST => 0,
        );

        if ( $ep['proxy'] ) {
            $opts[ CURLOPT_PROXY ]     = $ep['proxy'];
            $opts[ CURLOPT_PROXYTYPE ] = CURLPROXY_SOCKS5_HOSTNAME;
        }

        curl_setopt_array( $ch, $opts );

        $response  = curl_exec( $ch );
        $http_code = curl_getinfo( $ch, CURLINFO_HTTP_CODE );
        $err       = curl_error( $ch );
        curl_close( $ch );

        if ( $err ) {
            error_log( '[OnionPress Wayback] Curl error for ' . $url . ' via ' . $ep['url'] . ': ' . $err );
            continue; // Try next endpoint
        }

        error_log( '[OnionPress Wayback] Submitted ' . $url . ' — HTTP ' . $http_code );

        // Check for auth failure
        if ( $http_code === 401 || $http_code === 403 ) {
            $msg = @json_decode( $response, true );
            $reason = isset( $msg['message'] ) ? $msg['message'] : 'authentication required';
            error_log( '[OnionPress Wayback] Auth failed: ' . $reason . ' — trying next endpoint' );
            continue;
        }

        // Rate-limited — do NOT treat as success; queue for retry
        if ( $http_code === 429 ) {
            error_log( '[OnionPress Wayback] Rate-limited (HTTP 429) for ' . $url . ' — will queue for retry' );
            return false;
        }

        // Any 2xx/3xx response means the endpoint accepted it
        if ( $http_code >= 200 && $http_code < 400 ) {
            return true; // Success
        }

        // 4xx client error (other than auth/rate-limit) — log and try next
        if ( $http_code >= 400 && $http_code < 500 ) {
            error_log( '[OnionPress Wayback] Client error (HTTP ' . $http_code . ') for ' . $url . ', trying next endpoint' );
            continue;
        }

        // 5xx: server error, try next endpoint
        error_log( '[OnionPress Wayback] Server error (HTTP ' . $http_code . '), trying next endpoint' );
    }

    return false; // All endpoints failed
}

/**
 * Queue URLs for later Wayback archiving (deduplicated by URL).
 *
 * The menubar app polls this file and drains it when the onion service
 * is reachable (purple state).
 */
function onionpress_wayback_queue_urls( $urls ) {
    $queue_file = '/var/lib/onionpress/wayback-queue.json';

    // Read existing queue
    $queue = array();
    if ( file_exists( $queue_file ) ) {
        $data = @file_get_contents( $queue_file );
        if ( $data ) {
            $decoded = json_decode( $data, true );
            if ( is_array( $decoded ) ) {
                $queue = $decoded;
            }
        }
    }

    // Build set of existing URLs for dedup
    $existing = array();
    foreach ( $queue as $item ) {
        if ( isset( $item['url'] ) ) {
            $existing[ $item['url'] ] = true;
        }
    }

    // Add new URLs (deduplicated)
    $now = gmdate( 'Y-m-d\TH:i:s\Z' );
    foreach ( $urls as $url ) {
        if ( isset( $existing[ $url ] ) ) {
            // Update timestamp for existing entry
            foreach ( $queue as &$item ) {
                if ( $item['url'] === $url ) {
                    $item['queued_at'] = $now;
                    break;
                }
            }
            unset( $item );
        } else {
            $queue[] = array( 'url' => $url, 'queued_at' => $now );
            $existing[ $url ] = true;
        }
    }

    @file_put_contents( $queue_file, json_encode( $queue ) );
    error_log( '[OnionPress Wayback] Queued ' . count( $urls ) . ' URL(s) for later archiving (' . count( $queue ) . ' total in queue)' );
}

/**
 * Queue a post path for later archiving (when onion address becomes available).
 *
 * Stores the path; the wp_cron drain handler will resolve it to full .onion
 * (and clearnet) URLs once the onion_address file appears.
 */
function onionpress_wayback_queue_path( $path ) {
    $queue_file = '/var/lib/onionpress/wayback-queue.json';

    $queue = array();
    if ( file_exists( $queue_file ) ) {
        $data = @file_get_contents( $queue_file );
        if ( $data ) {
            $decoded = json_decode( $data, true );
            if ( is_array( $decoded ) ) {
                $queue = $decoded;
            }
        }
    }

    // Check for duplicate path
    foreach ( $queue as $item ) {
        if ( isset( $item['path'] ) && $item['path'] === $path ) {
            return; // Already queued
        }
    }

    $queue[] = array( 'path' => $path, 'queued_at' => gmdate( 'Y-m-d\TH:i:s\Z' ) );
    @file_put_contents( $queue_file, json_encode( $queue ) );
    error_log( '[OnionPress Wayback] Queued path ' . $path . ' for archiving once Tor is ready' );
}

// ── wp_cron queue drain ──────────────────────────────────────────────

/**
 * Register a 5-minute cron schedule for queue draining.
 */
add_filter( 'cron_schedules', function ( $schedules ) {
    $schedules['onionpress_every_5_minutes'] = array(
        'interval' => 300,
        'display'  => 'Every 5 minutes',
    );
    return $schedules;
} );

/**
 * Ensure the drain event is scheduled.
 */
add_action( 'init', function () {
    if ( ! wp_next_scheduled( 'onionpress_drain_wayback_queue' ) ) {
        wp_schedule_event( time(), 'onionpress_every_5_minutes', 'onionpress_drain_wayback_queue' );
    }
} );

/**
 * Drain one item from the Wayback queue.
 *
 * Processes one URL (or path) per run to stay within SPN rate limits.
 * Runs every 5 minutes via wp_cron — works on both macOS and Linux.
 */
add_action( 'onionpress_drain_wayback_queue', function () {
    $queue_file = '/var/lib/onionpress/wayback-queue.json';

    if ( ! file_exists( $queue_file ) ) {
        return;
    }

    $data = @file_get_contents( $queue_file );
    if ( ! $data ) {
        return;
    }

    $queue = json_decode( $data, true );
    if ( ! is_array( $queue ) || empty( $queue ) ) {
        return;
    }

    $item = $queue[0];

    // Handle path-only items (queued before onion address was available)
    if ( isset( $item['path'] ) && ! isset( $item['url'] ) ) {
        $onion_file = '/var/lib/onionpress/onion_address';
        if ( ! file_exists( $onion_file ) ) {
            return; // Still no onion address — try again next cycle
        }
        $onion_addr = trim( file_get_contents( $onion_file ) );
        if ( empty( $onion_addr ) ) {
            return;
        }

        // Resolve path to full URLs and re-queue them
        $path = $item['path'];
        $urls = array( 'http://' . $onion_addr . $path );

        // Also queue homepage and feed if the path isn't already one of them
        if ( $path !== '/' ) {
            $urls[] = 'http://' . $onion_addr . '/';
        }
        $urls[] = 'http://' . $onion_addr . '/feed/';

        // Add clearnet URLs if available
        $clearnet_file = '/var/lib/onionpress/clearnet_domain';
        if ( file_exists( $clearnet_file ) ) {
            $clearnet_domain = trim( file_get_contents( $clearnet_file ) );
            if ( ! empty( $clearnet_domain ) ) {
                $urls[] = 'https://' . $clearnet_domain . $path;
                if ( $path !== '/' ) {
                    $urls[] = 'https://' . $clearnet_domain . '/';
                }
                $urls[] = 'https://' . $clearnet_domain . '/feed/';
            }
        }

        // Remove the path item, queue the resolved URLs
        array_shift( $queue );
        @file_put_contents( $queue_file, json_encode( $queue ) );
        onionpress_wayback_queue_urls( array_unique( $urls ) );

        error_log( '[OnionPress Wayback] Resolved path ' . $path . ' to ' . count( $urls ) . ' URL(s)' );
        return;
    }

    // Handle normal URL items
    $url = isset( $item['url'] ) ? $item['url'] : '';
    if ( empty( $url ) ) {
        // Invalid item — remove and move on
        array_shift( $queue );
        @file_put_contents( $queue_file, json_encode( $queue ) );
        return;
    }

    $endpoints = array(
        array(
            'url'   => 'https://web.archive.org/save',
            'proxy' => null,
        ),
        array(
            'url'   => 'http://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save',
            'proxy' => 'socks5h://onionpress-tor:9050',
        ),
    );

    $auth = onionpress_wayback_auth_header();

    if ( onionpress_wayback_submit( $endpoints, $url, $auth ) ) {
        error_log( '[OnionPress Wayback] Queue drain: archived ' . $url );
        // Remove from queue
        array_shift( $queue );
        @file_put_contents( $queue_file, json_encode( $queue ) );
    } else {
        error_log( '[OnionPress Wayback] Queue drain: failed to archive ' . $url . ' — will retry next cycle' );
        // Leave in queue for next cycle. Move to end so other URLs get a chance.
        $item = array_shift( $queue );
        $queue[] = $item;
        @file_put_contents( $queue_file, json_encode( $queue ) );
    }
} );
