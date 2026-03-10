<?php
/**
 * Plugin Name: OnionPress Login Fix
 * Description: Fixes cookie-domain issues when logging in via localhost or onionpress
 *              hostname, and replaces unhelpful external help links with inline text.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Replace the "cookies blocked" error with helpful inline text.
 *
 * WordPress's test-cookie check can false-positive when the cookie domain
 * (the .onion address) differs from the hostname in the URL bar (localhost,
 * onionpress, etc.).  The default error links to wordpress.org which is
 * unreachable when offline.
 */
add_filter( 'login_errors', function ( $error ) {
    if ( stripos( $error, 'cookie' ) !== false ) {
        return '<strong>Tip:</strong> If you see a cookie error, try reloading '
             . 'this page and logging in again. Cookies work normally on this site.';
    }
    return $error;
} );

/**
 * Ensure the login redirect stays on the current hostname.
 *
 * After successful login, WordPress may redirect to the stored siteurl
 * (.onion address) even though the user logged in via localhost.  Rewrite
 * the redirect to match the current HTTP_HOST so the session continues
 * on the same hostname.
 */
add_filter( 'login_redirect', function ( $redirect_to ) {
    if ( ! isset( $_SERVER['HTTP_HOST'] ) ) {
        return $redirect_to;
    }

    $current_host = $_SERVER['HTTP_HOST'];

    // Only rewrite if the redirect points to a different host
    $parsed = wp_parse_url( $redirect_to );
    if ( isset( $parsed['host'] ) && $parsed['host'] !== $current_host ) {
        $redirect_to = preg_replace(
            '#//[^/]+#',
            '//' . $current_host,
            $redirect_to,
            1
        );
    }

    return $redirect_to;
}, 99 );
