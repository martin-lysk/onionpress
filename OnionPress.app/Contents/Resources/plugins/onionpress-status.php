<?php
/**
 * Plugin Name: OnionPress Status Page
 * Description: Public system health status page at /onionpress-status.
 *              Reads status data from the shared volume and optionally
 *              displays WP Statistics traffic summary.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Intercept requests to /onionpress-status and render the status page.
 */
add_action( 'template_redirect', function () {
    $path = trim( parse_url( $_SERVER['REQUEST_URI'], PHP_URL_PATH ), '/' );
    if ( $path !== 'onionpress-status' ) {
        return;
    }

    // Read status.json from the shared volume
    $status_file = '/var/lib/onionpress/status.json';
    $status = null;
    if ( file_exists( $status_file ) ) {
        $raw = file_get_contents( $status_file );
        if ( $raw !== false ) {
            $status = json_decode( $raw, true );
        }
    }

    // Gather WP Statistics data if the plugin is active
    $wp_stats = null;
    if ( function_exists( 'wp_statistics_pages' ) ) {
        $wp_stats = array(
            'total_views'    => (int) wp_statistics_pages( 'total', null, -1 ),
            'total_visitors' => (int) wp_statistics_visitor( 'total', null, true ),
            'today_views'    => (int) wp_statistics_pages( 'today', null, -1 ),
            'today_visitors' => (int) wp_statistics_visitor( 'today', null, true ),
        );

        // Daily page views for last 30 days (for the graph)
        $daily_views = array();
        for ( $i = 29; $i >= 0; $i-- ) {
            $date = date( 'Y-m-d', strtotime( "-{$i} days" ) );
            $count = (int) wp_statistics_pages( $date, null, -1 );
            $daily_views[] = array( 'date' => $date, 'views' => $count );
        }
        $wp_stats['daily_views'] = $daily_views;

        // Top 5 pages (use database directly for efficiency)
        global $wpdb;
        $table = $wpdb->prefix . 'statistics_pages';
        if ( $wpdb->get_var( "SHOW TABLES LIKE '{$table}'" ) === $table ) {
            $top_pages = $wpdb->get_results(
                "SELECT uri, SUM(count) as total FROM {$table} GROUP BY uri ORDER BY total DESC LIMIT 5",
                ARRAY_A
            );
            $wp_stats['top_pages'] = $top_pages ? $top_pages : array();
        }
    }

    header( 'Content-Type: text/html; charset=utf-8' );
    header( 'Cache-Control: no-cache, no-store, must-revalidate' );
    header( 'X-Robots-Tag: noindex' );

    onionpress_render_status_page( $status, $wp_stats );
    exit;
} );

/**
 * Render the standalone status page HTML.
 */
function onionpress_render_status_page( $status, $wp_stats ) {
    $state         = $status ? ( $status['state'] ?? 'unknown' ) : 'unknown';
    $version       = $status ? ( $status['version'] ?? '?' ) : '?';
    $onion_address = $status ? ( $status['onion_address'] ?? '' ) : '';
    $uptime        = $status ? ( $status['uptime_seconds'] ?? 0 ) : 0;
    $bootstrap     = $status ? ( $status['bootstrap_pct'] ?? 0 ) : 0;
    $containers    = $status ? ( $status['containers'] ?? array() ) : array();
    $wayback_queue = $status ? ( $status['wayback_queue_count'] ?? 0 ) : 0;
    $updated_at    = $status ? ( $status['updated_at'] ?? '' ) : '';

    // State color
    $state_colors = array(
        'running'  => '#8b5cf6', // purple
        'starting' => '#eab308', // yellow
        'stopped'  => '#6b7280', // gray
        'offline'  => '#6b7280',
        'stuck'    => '#ef4444', // red
        'unknown'  => '#6b7280',
    );
    $state_color = $state_colors[ $state ] ?? '#6b7280';

    // Format uptime
    $uptime_str = '';
    if ( $uptime > 0 ) {
        $days    = floor( $uptime / 86400 );
        $hours   = floor( ( $uptime % 86400 ) / 3600 );
        $minutes = floor( ( $uptime % 3600 ) / 60 );
        if ( $days > 0 ) {
            $uptime_str = "{$days}d {$hours}h {$minutes}m";
        } elseif ( $hours > 0 ) {
            $uptime_str = "{$hours}h {$minutes}m";
        } else {
            $uptime_str = "{$minutes}m";
        }
    }

    // Build daily views SVG graph
    $graph_svg = '';
    if ( $wp_stats && ! empty( $wp_stats['daily_views'] ) ) {
        $graph_svg = onionpress_render_traffic_graph( $wp_stats['daily_views'] );
    }

    ?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OnionPress Status</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background: #0f0f0f;
        color: #e5e5e5;
        padding: 2rem;
        max-width: 720px;
        margin: 0 auto;
    }
    h1 { font-size: 1.5rem; margin-bottom: 1.5rem; color: #fff; }
    h1 span { font-size: 0.75rem; color: #888; font-weight: normal; margin-left: 0.5rem; }
    .card {
        background: #1a1a1a;
        border: 1px solid #333;
        border-radius: 8px;
        padding: 1.25rem;
        margin-bottom: 1rem;
    }
    .card h2 { font-size: 0.875rem; color: #888; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.75rem; }
    .row { display: flex; justify-content: space-between; align-items: center; padding: 0.375rem 0; }
    .row + .row { border-top: 1px solid #262626; }
    .label { color: #999; font-size: 0.875rem; }
    .value { color: #e5e5e5; font-size: 0.875rem; font-weight: 500; }
    .state-dot {
        display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 0.4rem; vertical-align: middle;
    }
    .onion-addr {
        font-family: "SF Mono", "Fira Code", "Consolas", monospace;
        font-size: 0.75rem;
        word-break: break-all;
        color: #c084fc;
    }
    .container-ok { color: #4ade80; }
    .container-bad { color: #f87171; }
    .stat-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.75rem; }
    .stat-box { text-align: center; }
    .stat-box .num { font-size: 1.5rem; font-weight: 700; color: #fff; }
    .stat-box .lbl { font-size: 0.75rem; color: #888; margin-top: 0.125rem; }
    .top-pages { list-style: none; }
    .top-pages li { display: flex; justify-content: space-between; padding: 0.25rem 0; font-size: 0.8125rem; }
    .top-pages li + li { border-top: 1px solid #262626; }
    .top-pages .pg-uri { color: #c084fc; max-width: 80%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .top-pages .pg-count { color: #888; }
    .graph-wrap { margin-top: 0.5rem; }
    .updated { text-align: center; font-size: 0.75rem; color: #555; margin-top: 1rem; }
    .no-data { color: #555; font-size: 0.875rem; text-align: center; padding: 2rem 0; }
</style>
</head>
<body>
<h1>OnionPress Status <span>v<?php echo esc_html( $version ); ?></span></h1>

<?php if ( ! $status ) : ?>
    <div class="card"><p class="no-data">Status data not available yet. The system may still be starting up.</p></div>
<?php else : ?>

<div class="card">
    <h2>System</h2>
    <div class="row">
        <span class="label">State</span>
        <span class="value"><span class="state-dot" style="background:<?php echo $state_color; ?>"></span><?php echo esc_html( ucfirst( $state ) ); ?></span>
    </div>
    <?php if ( $onion_address && strpos( $onion_address, '.onion' ) !== false ) : ?>
    <div class="row">
        <span class="label">Onion Address</span>
        <span class="value onion-addr"><?php echo esc_html( $onion_address ); ?></span>
    </div>
    <?php endif; ?>
    <div class="row">
        <span class="label">Uptime</span>
        <span class="value"><?php echo $uptime_str ? esc_html( $uptime_str ) : '—'; ?></span>
    </div>
    <div class="row">
        <span class="label">Bootstrap</span>
        <span class="value"><?php echo (int) $bootstrap; ?>%</span>
    </div>
    <?php if ( $wayback_queue > 0 ) : ?>
    <div class="row">
        <span class="label">Wayback Queue</span>
        <span class="value"><?php echo (int) $wayback_queue; ?> item<?php echo $wayback_queue != 1 ? 's' : ''; ?></span>
    </div>
    <?php endif; ?>
</div>

<div class="card">
    <h2>Containers</h2>
    <?php if ( empty( $containers ) ) : ?>
        <p class="no-data">No container data</p>
    <?php else : ?>
        <?php foreach ( $containers as $name => $cstate ) : ?>
        <div class="row">
            <span class="label"><?php echo esc_html( $name ); ?></span>
            <span class="value <?php echo $cstate === 'running' ? 'container-ok' : 'container-bad'; ?>"><?php echo esc_html( ucfirst( $cstate ) ); ?></span>
        </div>
        <?php endforeach; ?>
    <?php endif; ?>
</div>

<?php if ( $wp_stats ) : ?>
<div class="card">
    <h2>Traffic</h2>
    <div class="stat-grid">
        <div class="stat-box">
            <div class="num"><?php echo number_format( $wp_stats['total_views'] ); ?></div>
            <div class="lbl">Total Views</div>
        </div>
        <div class="stat-box">
            <div class="num"><?php echo number_format( $wp_stats['total_visitors'] ); ?></div>
            <div class="lbl">Total Visitors</div>
        </div>
        <div class="stat-box">
            <div class="num"><?php echo number_format( $wp_stats['today_views'] ); ?></div>
            <div class="lbl">Views Today</div>
        </div>
        <div class="stat-box">
            <div class="num"><?php echo number_format( $wp_stats['today_visitors'] ); ?></div>
            <div class="lbl">Visitors Today</div>
        </div>
    </div>

    <?php if ( $graph_svg ) : ?>
    <div class="graph-wrap">
        <h2 style="margin-top:1rem">Daily Views (30 days)</h2>
        <?php echo $graph_svg; ?>
    </div>
    <?php endif; ?>

    <?php if ( ! empty( $wp_stats['top_pages'] ) ) : ?>
    <h2 style="margin-top:1rem">Top Pages</h2>
    <ul class="top-pages">
        <?php foreach ( $wp_stats['top_pages'] as $page ) : ?>
        <li>
            <span class="pg-uri"><?php echo esc_html( $page['uri'] ?: '/' ); ?></span>
            <span class="pg-count"><?php echo number_format( (int) $page['total'] ); ?></span>
        </li>
        <?php endforeach; ?>
    </ul>
    <?php endif; ?>
</div>
<?php endif; ?>

<?php endif; ?>

<?php if ( $updated_at ) : ?>
<p class="updated">Last updated: <?php echo esc_html( $updated_at ); ?></p>
<?php endif; ?>

</body>
</html>
    <?php
}

/**
 * Render an inline SVG line graph of daily page views.
 */
function onionpress_render_traffic_graph( $daily_views ) {
    $width  = 660;
    $height = 120;
    $pad_x  = 0;
    $pad_y  = 10;

    $max_val = 1;
    foreach ( $daily_views as $d ) {
        if ( $d['views'] > $max_val ) {
            $max_val = $d['views'];
        }
    }

    $count  = count( $daily_views );
    $step_x = ( $width - 2 * $pad_x ) / max( $count - 1, 1 );
    $usable = $height - 2 * $pad_y;

    $points = array();
    $fill_points = array();
    $fill_points[] = $pad_x . ',' . ( $height - $pad_y );

    foreach ( $daily_views as $i => $d ) {
        $x = $pad_x + $i * $step_x;
        $y = ( $height - $pad_y ) - ( $d['views'] / $max_val ) * $usable;
        $points[] = round( $x, 1 ) . ',' . round( $y, 1 );
        $fill_points[] = round( $x, 1 ) . ',' . round( $y, 1 );
    }

    $fill_points[] = round( $pad_x + ( $count - 1 ) * $step_x, 1 ) . ',' . ( $height - $pad_y );

    $polyline_str = implode( ' ', $points );
    $fill_str     = implode( ' ', $fill_points );

    $svg  = '<svg viewBox="0 0 ' . $width . ' ' . $height . '" style="width:100%;height:auto;margin-top:0.5rem">';
    $svg .= '<polygon points="' . $fill_str . '" fill="rgba(139,92,246,0.15)" />';
    $svg .= '<polyline points="' . $polyline_str . '" fill="none" stroke="#8b5cf6" stroke-width="2" stroke-linejoin="round" />';
    $svg .= '</svg>';

    return $svg;
}
