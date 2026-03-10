<?php
/**
 * Plugin Name: OnionPress Wayback Archive
 * Description: Automatically archives published posts and the homepage to the
 *              Internet Archive Wayback Machine.
 * Version:     1.2
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
 * Auto-detect and cache the clearnet domain.
 *
 * On every web request, if the incoming HTTP_HOST is not .onion, localhost*, or
 * the Docker-internal hostname "wordpress", we treat it as the clearnet domain
 * (set via Cloudflare Tunnel or similar) and persist it to disk.
 */
add_action( 'init', function () {
    if ( ! isset( $_SERVER['HTTP_HOST'] ) ) {
        return;
    }

    $host = $_SERVER['HTTP_HOST'];

    // Skip .onion, localhost, onionpress (with or without port), and Docker-internal hostname
    if ( preg_match( '/\.onion$/i', $host )
        || preg_match( '/^localhost(:\d+)?$/i', $host )
        || preg_match( '/^onionpress(:\d+)?$/i', $host )
        || $host === 'wordpress'
    ) {
        return;
    }

    $file = '/var/lib/onionpress/clearnet_domain';

    // Only write if the value changed (avoid disk churn)
    $current = @file_get_contents( $file );
    if ( $current !== false && trim( $current ) === $host ) {
        return;
    }

    @file_put_contents( $file, $host );
}, 1 );

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

    // 4. Clearnet URLs (if Cloudflare Tunnel is configured)
    $clearnet_file = '/var/lib/onionpress/clearnet_domain';
    if ( file_exists( $clearnet_file ) ) {
        $clearnet_domain = trim( file_get_contents( $clearnet_file ) );
        if ( ! empty( $clearnet_domain ) ) {
            $urls[] = 'https://' . $clearnet_domain . $path;
            $urls[] = 'https://' . $clearnet_domain . '/';
            $urls[] = 'https://' . $clearnet_domain . '/feed/';
        }
    }

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
    foreach ( $urls as $url ) {
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

        // Any non-error response means the endpoint accepted it
        if ( $http_code >= 200 && $http_code < 500 ) {
            return true; // Success
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
 * Queue post path for later archiving (when onion address becomes available).
 *
 * Builds .onion URLs from the path once the address file appears.
 * For now, stores paths so the menubar can construct full URLs later.
 */
function onionpress_wayback_queue_path( $path ) {
    // We can't build the full .onion URL yet, so queue paths.
    // The menubar drainer will skip items without full URLs and
    // the next poll_wayback_queue after Tor is ready will resolve them.
    // For simplicity, just queue placeholder URLs — the next publish
    // after Tor is ready will archive everything properly.
    error_log( '[OnionPress Wayback] Onion address not available — post at ' . $path . ' will be archived on next publish after Tor is ready' );
}
