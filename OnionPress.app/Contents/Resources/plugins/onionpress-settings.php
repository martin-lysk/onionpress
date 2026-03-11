<?php
/**
 * Plugin Name: OnionPress Settings
 * Description: Admin settings page for remote OnionPress configuration.
 *              Reads current config from the shared volume and writes
 *              updates for the menubar app to pick up.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Register the admin menu page.
 */
add_action( 'admin_menu', function () {
    add_menu_page(
        'OnionPress Settings',
        'OnionPress',
        'manage_options',
        'onionpress-settings',
        'onionpress_settings_page',
        'dashicons-shield',
        80
    );
} );

// For multisite, also add to network admin
add_action( 'network_admin_menu', function () {
    add_menu_page(
        'OnionPress Settings',
        'OnionPress',
        'manage_network_options',
        'onionpress-settings',
        'onionpress_settings_page',
        'dashicons-shield',
        80
    );
} );

/**
 * Handle service control actions (restart/stop) on Linux.
 */
add_action( 'admin_init', function () {
    if ( ! isset( $_POST['onionpress_action_nonce'] ) ) {
        return;
    }
    if ( ! wp_verify_nonce( $_POST['onionpress_action_nonce'], 'onionpress_action' ) ) {
        return;
    }
    if ( ! current_user_can( 'manage_options' ) ) {
        return;
    }
    $action = sanitize_text_field( $_POST['onionpress_action'] ?? '' );
    if ( ! in_array( $action, array( 'restart', 'stop', 'save-restart' ), true ) ) {
        return;
    }

    // If this is a save-restart, also save config updates first
    if ( $action === 'save-restart' ) {
        $_POST['onionpress_settings_nonce'] = wp_create_nonce( 'onionpress_settings_save' );
        // The settings handler below will fire on the same request
    }

    $file = '/var/lib/onionpress/requested-action';
    $cmd = ( $action === 'stop' ) ? 'stop' : 'restart';
    if ( @file_put_contents( $file, $cmd ) === false ) {
        add_action( 'admin_notices', function () {
            echo '<div class="notice notice-error"><p>Failed to write action request. The shared volume may not be mounted.</p></div>';
        } );
        return;
    }

    $label = ( $action === 'stop' ) ? 'Stopping' : 'Restarting';
    add_action( 'admin_notices', function () use ( $label ) {
        echo '<div class="notice notice-info is-dismissible"><p>' . esc_html( $label ) . ' OnionPress... This may take a minute. The page will become unavailable during restart.</p></div>';
    } );
} );

/**
 * Handle form submission — write config-updates.json to the shared volume.
 */
add_action( 'admin_init', function () {
    if ( ! isset( $_POST['onionpress_settings_nonce'] ) ) {
        return;
    }
    if ( ! wp_verify_nonce( $_POST['onionpress_settings_nonce'], 'onionpress_settings_save' ) ) {
        add_action( 'admin_notices', function () {
            echo '<div class="notice notice-error"><p>Security check failed. Please try again.</p></div>';
        } );
        return;
    }
    if ( ! current_user_can( 'manage_options' ) ) {
        return;
    }

    $updates = array();
    $fields  = onionpress_settings_fields();

    foreach ( $fields as $key => $field ) {
        if ( ! isset( $_POST[ 'op_' . $key ] ) ) {
            continue;
        }
        $val = sanitize_text_field( wp_unslash( $_POST[ 'op_' . $key ] ) );

        // Validate against allowed values if specified
        if ( ! empty( $field['options'] ) && ! array_key_exists( $val, $field['options'] ) ) {
            continue;
        }

        $updates[ $key ] = $val;
    }

    // Handle Wayback Machine S3 credentials (stored in wp_options, not config file)
    $wayback_fields = array(
        'archive_s3_access' => 'onionpress_archive_s3_access',
        'archive_s3_secret' => 'onionpress_archive_s3_secret',
    );
    foreach ( $wayback_fields as $post_key => $option_key ) {
        if ( isset( $_POST[ 'op_' . $post_key ] ) ) {
            $val = sanitize_text_field( wp_unslash( $_POST[ 'op_' . $post_key ] ) );
            update_option( $option_key, $val );
        }
    }

    if ( ! empty( $updates ) ) {
        $json = json_encode( $updates, JSON_PRETTY_PRINT );
        $file = '/var/lib/onionpress/config-updates.json';
        if ( @file_put_contents( $file, $json ) === false ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Failed to write config update. The shared volume may not be mounted.</p></div>';
            } );
            return;
        }
    }

    // Redirect with success message
    add_action( 'admin_notices', function () {
        $is_linux = false;
        $sf = '/var/lib/onionpress/status.json';
        if ( file_exists( $sf ) ) {
            $sr = file_get_contents( $sf );
            if ( $sr !== false ) {
                $sd = json_decode( $sr, true );
                if ( is_array( $sd ) && isset( $sd['platform'] ) && $sd['platform'] === 'linux' ) {
                    $is_linux = true;
                }
            }
        }
        if ( $is_linux ) {
            echo '<div class="notice notice-success is-dismissible"><p>Settings saved. Restart OnionPress for changes to take effect: <code>sudo systemctl restart onionpress</code></p></div>';
        } else {
            echo '<div class="notice notice-success is-dismissible"><p>Settings saved. Changes will take effect within 30 seconds.</p></div>';
        }
    } );
} );

/**
 * Define the settings fields and their metadata.
 */
function onionpress_settings_fields() {
    // Detect platform from status.json
    $is_linux = false;
    $status_file = '/var/lib/onionpress/status.json';
    if ( file_exists( $status_file ) ) {
        $raw = file_get_contents( $status_file );
        if ( $raw !== false ) {
            $s = json_decode( $raw, true );
            if ( is_array( $s ) && isset( $s['platform'] ) && $s['platform'] === 'linux' ) {
                $is_linux = true;
            }
        }
    }

    return array(
        'ADDRESS_PREFIX' => array(
            'label'       => 'Onion Address Prefix',
            'description' => 'Customize the beginning of your .onion address (base32: a-z, 2-7, max 5 chars). Changing this generates a new address — your old address will stop working.',
            'type'        => 'text',
            'placeholder' => 'op2',
        ),
        'INSTALL_IA_PLUGIN' => array(
            'label'       => 'Internet Archive Link Fixer',
            'description' => 'Automatically install and activate the Wayback Machine Link Fixer plugin.',
            'type'        => 'select',
            'options'     => array( 'yes' => 'Enabled', 'no' => 'Disabled' ),
        ),
        'INSTALL_WP_STATISTICS' => array(
            'label'       => 'WP Statistics',
            'description' => 'Automatically install WP Statistics for privacy-friendly traffic analytics.',
            'type'        => 'select',
            'options'     => array( 'yes' => 'Enabled', 'no' => 'Disabled' ),
        ),
        'UPDATE_ON_LAUNCH' => array(
            'label'       => 'Update on Launch',
            'description' => 'Automatically check for and download updated Docker images when the app launches.',
            'type'        => 'select',
            'options'     => array( 'yes' => 'Enabled', 'no' => 'Disabled' ),
        ),
        'START_ON_BOOT' => array(
            'label'       => $is_linux ? 'Start on Boot' : 'Launch on Login',
            'description' => $is_linux
                ? 'Automatically start OnionPress when the system boots.'
                : 'Automatically start OnionPress when you log in to macOS.',
            'type'        => 'select',
            'options'     => array( 'yes' => 'Enabled', 'no' => 'Disabled' ),
            'config_key'  => $is_linux ? 'START_ON_BOOT' : 'LAUNCH_ON_LOGIN',
        ),
        'PREVENT_SLEEP' => array(
            'label'       => 'Sleep Prevention',
            'description' => 'Control whether OnionPress keeps your Mac awake while running.',
            'type'        => 'select',
            'options'     => array(
                'normal'     => 'Normal (Mac sleeps as usual)',
                'on-battery' => 'On Battery (stay awake on AC power)',
                'never'      => 'Never (always stay awake)',
            ),
            'platform'    => 'macos',
        ),
        'VM_MEMORY' => array(
            'label'       => 'VM Memory (GB)',
            'description' => 'RAM allocated to the container VM. Requires restart to take effect.',
            'type'        => 'text',
            'placeholder' => '1',
            'platform'    => 'macos',
        ),
        'CLOUDFLARE_TUNNEL_TOKEN' => array(
            'label'       => 'Cloudflare Tunnel Token',
            'description' => 'Expose your site on the regular internet via Cloudflare Tunnel. Privacy note: this reveals your IP to Cloudflare.',
            'type'        => 'text',
            'placeholder' => '',
        ),
        'REGISTER_WITH_ONIONHEAVEN' => array(
            'label'       => 'Register with OnionHeaven',
            'description' => 'Register your site with OnionHeaven for Wayback Machine fallback when offline.',
            'type'        => 'select',
            'options'     => array( 'yes' => 'Enabled', 'no' => 'Disabled' ),
        ),
    );
}

/**
 * Render the settings page.
 */
function onionpress_settings_page() {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized' );
    }

    // Read current config from the shared volume
    $current = array();
    $config_file = '/var/lib/onionpress/config-current.json';
    if ( file_exists( $config_file ) ) {
        $raw = file_get_contents( $config_file );
        if ( $raw !== false ) {
            $decoded = json_decode( $raw, true );
            if ( is_array( $decoded ) ) {
                $current = $decoded;
            }
        }
    }

    // Read Wayback S3 credentials from wp_options
    $s3_access = get_option( 'onionpress_archive_s3_access', '' );
    $s3_secret = get_option( 'onionpress_archive_s3_secret', '' );

    // Read status for version display
    $status_file = '/var/lib/onionpress/status.json';
    $status = null;
    if ( file_exists( $status_file ) ) {
        $raw = file_get_contents( $status_file );
        if ( $raw !== false ) {
            $status = json_decode( $raw, true );
        }
    }

    $fields = onionpress_settings_fields();

    ?>
    <div class="wrap">
        <h1>OnionPress Settings</h1>
        <?php if ( $status ) : ?>
        <p class="description">
            Version <?php echo esc_html( $status['version'] ?? '?' ); ?>
            &mdash; State: <?php echo esc_html( ucfirst( $status['state'] ?? 'unknown' ) ); ?>
            <?php if ( ! empty( $status['onion_address'] ) && strpos( $status['onion_address'], '.onion' ) !== false ) : ?>
                &mdash; <code><?php echo esc_html( $status['onion_address'] ); ?></code>
            <?php endif; ?>
        </p>
        <?php endif; ?>

        <form method="post">
            <?php wp_nonce_field( 'onionpress_settings_save', 'onionpress_settings_nonce' ); ?>
            <table class="form-table" role="presentation">
                <?php
                // Detect platform for field filtering
                $current_platform = 'macos';
                $sf = '/var/lib/onionpress/status.json';
                if ( file_exists( $sf ) ) {
                    $sr = file_get_contents( $sf );
                    if ( $sr !== false ) {
                        $sd = json_decode( $sr, true );
                        if ( is_array( $sd ) && isset( $sd['platform'] ) ) {
                            $current_platform = $sd['platform'];
                        }
                    }
                }
                ?>
                <?php foreach ( $fields as $key => $field ) : ?>
                <?php
                // Skip fields restricted to a different platform
                if ( ! empty( $field['platform'] ) && $field['platform'] !== $current_platform ) {
                    continue;
                }
                // Use config_key if specified (for renamed settings like LAUNCH_ON_LOGIN -> START_ON_BOOT)
                $config_key = ! empty( $field['config_key'] ) ? $field['config_key'] : $key;
                ?>
                <tr>
                    <th scope="row"><label for="op_<?php echo esc_attr( $key ); ?>"><?php echo esc_html( $field['label'] ); ?></label></th>
                    <td>
                        <?php
                        $val = $current[ $config_key ] ?? ( $current[ $key ] ?? '' );
                        if ( $field['type'] === 'select' && ! empty( $field['options'] ) ) :
                        ?>
                            <select name="op_<?php echo esc_attr( $key ); ?>" id="op_<?php echo esc_attr( $key ); ?>">
                                <?php foreach ( $field['options'] as $opt_val => $opt_label ) : ?>
                                <option value="<?php echo esc_attr( $opt_val ); ?>" <?php selected( $val, $opt_val ); ?>>
                                    <?php echo esc_html( $opt_label ); ?>
                                </option>
                                <?php endforeach; ?>
                            </select>
                        <?php else : ?>
                            <input type="text" name="op_<?php echo esc_attr( $key ); ?>" id="op_<?php echo esc_attr( $key ); ?>"
                                   value="<?php echo esc_attr( $val ); ?>"
                                   placeholder="<?php echo esc_attr( $field['placeholder'] ?? '' ); ?>"
                                   class="regular-text">
                        <?php endif; ?>
                        <p class="description"><?php echo esc_html( $field['description'] ); ?></p>
                    </td>
                </tr>
                <?php endforeach; ?>

                <!-- Wayback Machine S3 Credentials (stored in wp_options) -->
                <tr>
                    <th scope="row"><label for="op_archive_s3_access">Wayback S3 Access Key</label></th>
                    <td>
                        <input type="text" name="op_archive_s3_access" id="op_archive_s3_access"
                               value="<?php echo esc_attr( $s3_access ); ?>" class="regular-text">
                        <p class="description">archive.org S3 API access key for authenticated Wayback Machine archiving.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="op_archive_s3_secret">Wayback S3 Secret Key</label></th>
                    <td>
                        <input type="password" name="op_archive_s3_secret" id="op_archive_s3_secret"
                               value="<?php echo esc_attr( $s3_secret ); ?>" class="regular-text">
                        <p class="description">archive.org S3 API secret key.</p>
                    </td>
                </tr>
            </table>

            <?php submit_button( 'Save Settings' ); ?>

            <?php if ( $current_platform !== 'linux' ) : ?>
            <p class="description">
                Settings are picked up by the OnionPress menubar app within 30 seconds.
                Some settings (VM Memory, Address Prefix) require a restart to take effect.
            </p>
            <?php endif; ?>
        </form>

        <?php if ( $current_platform === 'linux' ) : ?>
        <hr>
        <h2>Service Control</h2>
        <p class="description">Some settings (Address Prefix, Cloudflare Tunnel) require a restart to take effect.</p>
        <form method="post" style="display: inline-block; margin-right: 10px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="restart">
            <?php submit_button( 'Restart OnionPress', 'primary', 'submit', false ); ?>
        </form>
        <form method="post" style="display: inline-block;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="stop">
            <?php submit_button( 'Stop OnionPress', 'secondary', 'submit', false ); ?>
        </form>
        <p class="description" style="margin-top: 10px;">
            Restart will apply any saved settings changes. The page will be unavailable briefly during restart.
        </p>
        <?php endif; ?>
    </div>
    <?php
}
