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
    if ( ! in_array( $action, array( 'restart', 'stop', 'start', 'save-restart', 'check-reachability', 'generate-vanity', 'import-key-file' ), true ) ) {
        return;
    }

    // If this is a save-restart, also save config updates first
    if ( $action === 'save-restart' ) {
        $_POST['onionpress_settings_nonce'] = wp_create_nonce( 'onionpress_settings_save' );
        // The settings handler below will fire on the same request
    }

    // Handle key file upload for import-key-file action
    if ( $action === 'import-key-file' ) {
        if ( ! empty( $_POST['op_key_b32'] ) ) {
            // Base32-encoded key pasted directly
            $key_data = sanitize_text_field( wp_unslash( $_POST['op_key_b32'] ) );
            if ( @file_put_contents( '/var/lib/onionpress/import-key-data', $key_data ) === false ) {
                add_action( 'admin_notices', function () {
                    echo '<div class="notice notice-error"><p>Failed to write key data.</p></div>';
                } );
                return;
            }
        } elseif ( ! empty( $_FILES['op_key_file'] ) && $_FILES['op_key_file']['error'] === UPLOAD_ERR_OK ) {
            // Binary key file uploaded — encode to base32
            $raw = file_get_contents( $_FILES['op_key_file']['tmp_name'] );
            if ( $raw === false || strlen( $raw ) < 64 ) {
                add_action( 'admin_notices', function () {
                    echo '<div class="notice notice-error"><p>Invalid key file. Expected at least 64 bytes.</p></div>';
                } );
                return;
            }
            $key_data = base64_encode( $raw ); // PHP doesn't have base32, use base64 then convert
            // Use Python-compatible base32 encoding
            $key_data = trim( shell_exec( "echo " . escapeshellarg( bin2hex( $raw ) ) . " | python3 -c \"import base64,sys; print(base64.b32encode(bytes.fromhex(sys.stdin.read().strip())).decode())\"" ) );
            if ( empty( $key_data ) ) {
                add_action( 'admin_notices', function () {
                    echo '<div class="notice notice-error"><p>Failed to encode key file.</p></div>';
                } );
                return;
            }
            if ( @file_put_contents( '/var/lib/onionpress/import-key-data', $key_data ) === false ) {
                add_action( 'admin_notices', function () {
                    echo '<div class="notice notice-error"><p>Failed to write key data.</p></div>';
                } );
                return;
            }
        } else {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Please provide a key — either paste the base32 key or upload the key file.</p></div>';
            } );
            return;
        }
    }

    $file = '/var/lib/onionpress/requested-action';

    $cmd_map = array(
        'stop' => 'stop', 'start' => 'start', 'restart' => 'restart', 'save-restart' => 'restart',
        'check-reachability' => 'check-reachability', 'generate-vanity' => 'generate-vanity',
        'import-key-file' => 'import-key-file',
    );
    $cmd = $cmd_map[ $action ];
    if ( @file_put_contents( $file, $cmd ) === false ) {
        add_action( 'admin_notices', function () {
            echo '<div class="notice notice-error"><p>Failed to write action request. The shared volume may not be mounted.</p></div>';
        } );
        return;
    }

    $label_map = array(
        'stop' => 'Stopping', 'start' => 'Starting', 'restart' => 'Restarting', 'save-restart' => 'Restarting',
        'check-reachability' => 'Testing Tor reachability for',
        'generate-vanity' => 'Generating vanity address for',
        'import-key-file' => 'Importing key for',
    );
    $label = $label_map[ $action ] ?? 'Processing';
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

    // Minimal status values for the status bar
    $state         = $status ? ( $status['state'] ?? 'unknown' ) : 'unknown';
    $onion_address = $status ? ( $status['onion_address'] ?? '' ) : '';

    $state_colors = array(
        'running'  => '#4ade80',
        'starting' => '#eab308',
        'stopped'  => '#9ca3af',
        'unknown'  => '#9ca3af',
    );
    $state_color = $state_colors[ $state ] ?? '#9ca3af';

    ?>
    <style>
        .onionpress-state-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 6px;
            vertical-align: middle;
        }
    </style>
    <?php
    $current_platform = isset( $status['platform'] ) ? $status['platform'] : 'macos';
    ?>
    <div class="wrap">
        <h1>OnionPress Settings</h1>

        <p style="margin-bottom: 16px; font-size: 14px;">
            <span class="onionpress-state-dot" style="background:<?php echo esc_attr( $state_color ); ?>"></span>
            <strong><?php echo esc_html( ucfirst( $state ) ); ?></strong>
            <?php if ( $onion_address && strpos( $onion_address, '.onion' ) !== false ) : ?>
                &mdash; <code style="font-size:12px;color:#8b5cf6;"><?php echo esc_html( $onion_address ); ?></code>
            <?php endif; ?>
            &mdash; <a href="/onionpress-status">View full status &amp; logs &rarr;</a>
        </p>

        <form method="post">
            <?php wp_nonce_field( 'onionpress_settings_save', 'onionpress_settings_nonce' ); ?>
            <table class="form-table" role="presentation">
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
        <form method="post" style="display: inline-block; margin-right: 10px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="stop">
            <?php submit_button( 'Stop OnionPress', 'secondary', 'submit', false ); ?>
        </form>
        <p class="description" style="margin-top: 10px;">
            Restart will apply any saved settings changes. The page will be unavailable briefly during restart.<br>
            To start after a full stop, use SSH: <code>sudo systemctl start onionpress</code>
        </p>

        <!-- Tor Reachability Test -->
        <hr>
        <h2>Tor Reachability Test</h2>
        <?php
        $reach_file = '/var/lib/onionpress/reachability-result.json';
        if ( file_exists( $reach_file ) ) {
            $reach = json_decode( file_get_contents( $reach_file ), true );
            if ( is_array( $reach ) ) {
                $reach_color = ! empty( $reach['reachable'] ) ? '#16a34a' : '#dc2626';
                $reach_label = ! empty( $reach['reachable'] ) ? 'Reachable' : 'Not reachable';
                echo '<p><strong style="color:' . esc_attr( $reach_color ) . '">' . esc_html( $reach_label ) . '</strong>';
                if ( ! empty( $reach['http_code'] ) ) {
                    echo ' (HTTP ' . esc_html( $reach['http_code'] ) . ')';
                }
                if ( ! empty( $reach['error'] ) ) {
                    echo ' &mdash; ' . esc_html( $reach['error'] );
                }
                if ( ! empty( $reach['tested_at'] ) ) {
                    echo ' <span class="description">&mdash; tested ' . esc_html( $reach['tested_at'] ) . '</span>';
                }
                echo '</p>';
            }
        }
        ?>
        <p class="description">Test whether your onion service is accessible from the Tor network. This takes up to 60 seconds.</p>
        <form method="post" style="margin-top: 8px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="check-reachability">
            <?php submit_button( 'Test Reachability', 'secondary', 'submit', false ); ?>
        </form>

        <!-- Vanity Address Generation -->
        <hr>
        <h2>Vanity Address Generation</h2>
        <?php
        $vanity_file = '/var/lib/onionpress/vanity-result.json';
        if ( file_exists( $vanity_file ) ) {
            $vanity = json_decode( file_get_contents( $vanity_file ), true );
            if ( is_array( $vanity ) ) {
                if ( ! empty( $vanity['success'] ) ) {
                    echo '<p><strong style="color:#16a34a">Generated:</strong> <code>' . esc_html( $vanity['address'] ?? '' ) . '</code></p>';
                } elseif ( ! empty( $vanity['error'] ) ) {
                    echo '<p><strong style="color:#dc2626">Error:</strong> ' . esc_html( $vanity['error'] ) . '</p>';
                }
            }
        }
        ?>
        <p class="description">Generate a new vanity .onion address matching your Address Prefix setting. This will <strong>replace</strong> your current address. Generation can take seconds to minutes depending on prefix length.</p>
        <form method="post" style="margin-top: 8px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="generate-vanity">
            <?php submit_button( 'Generate Vanity Address', 'secondary', 'submit', false ); ?>
        </form>

        <!-- Import Key -->
        <hr>
        <h2>Import Onion Service Key</h2>
        <?php
        $import_file = '/var/lib/onionpress/import-result.json';
        if ( file_exists( $import_file ) ) {
            $import_result = json_decode( file_get_contents( $import_file ), true );
            if ( is_array( $import_result ) ) {
                if ( ! empty( $import_result['success'] ) ) {
                    echo '<p><strong style="color:#16a34a">Imported:</strong> <code>' . esc_html( $import_result['address'] ?? '' ) . '</code></p>';
                } elseif ( ! empty( $import_result['error'] ) ) {
                    echo '<p><strong style="color:#dc2626">Error:</strong> ' . esc_html( $import_result['error'] ) . '</p>';
                }
            }
        }
        ?>
        <p class="description">Import a pre-generated ed25519 key. This will <strong>replace</strong> your current onion address. Paste the base32-encoded key or upload the <code>hs_ed25519_secret_key</code> file.</p>
        <form method="post" enctype="multipart/form-data" style="margin-top: 8px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="import-key-file">
            <table class="form-table" role="presentation">
                <tr>
                    <th scope="row"><label for="op_key_b32">Base32 Key</label></th>
                    <td>
                        <input type="text" name="op_key_b32" id="op_key_b32" class="large-text" placeholder="Paste base32-encoded key here...">
                        <p class="description">The base32-encoded 64-byte expanded ed25519 secret key.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="op_key_file">Or Upload Key File</label></th>
                    <td>
                        <input type="file" name="op_key_file" id="op_key_file">
                        <p class="description">Upload <code>hs_ed25519_secret_key</code> from mkp224o output or a backup.</p>
                    </td>
                </tr>
            </table>
            <?php submit_button( 'Import Key', 'secondary', 'submit', false ); ?>
        </form>

        <?php endif; ?>
    </div>
    <?php
}
