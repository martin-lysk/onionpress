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
 * Convert Archive.org account + password into S3 API keys via xauthn.
 *
 * @return array{'access':string,'secret':string}|string  Keys on success, error message on failure.
 */
function onionpress_fetch_ia_s3_keys( $email, $password ) {
    $resp = wp_remote_post( 'https://archive.org/services/xauthn/?op=login', array(
        'timeout' => 15,
        'headers' => array( 'User-Agent' => 'OnionPress (+https://github.com/brewsterkahle/onionpress)' ),
        'body'    => array( 'email' => $email, 'password' => $password ),
    ) );
    if ( is_wp_error( $resp ) ) {
        return $resp->get_error_message();
    }
    $data = json_decode( wp_remote_retrieve_body( $resp ), true );
    if ( empty( $data['success'] ) ) {
        return $data['values']['reason'] ?? 'Login failed';
    }

    $access = $data['values']['s3']['access'] ?? '';
    $secret = $data['values']['s3']['secret'] ?? '';
    if ( $access && $secret ) {
        return array( 'access' => $access, 'secret' => $secret );
    }

    // Fallback: fetch S3 keys separately using returned cookies
    $sig  = $data['values']['cookies']['logged-in-sig'] ?? '';
    $user = $data['values']['cookies']['logged-in-user'] ?? '';
    if ( $sig && $user ) {
        $resp2 = wp_remote_get( 'https://archive.org/account/s3.php?output_json=1', array(
            'timeout' => 15,
            'headers' => array(
                'User-Agent' => 'OnionPress (+https://github.com/brewsterkahle/onionpress)',
                'Cookie'     => "logged-in-sig=$sig; logged-in-user=$user",
            ),
        ) );
        if ( ! is_wp_error( $resp2 ) ) {
            $s3 = json_decode( wp_remote_retrieve_body( $resp2 ), true );
            $access = $s3['key']['s3accesskey'] ?? '';
            $secret = $s3['key']['s3secretkey'] ?? '';
            if ( $access && $secret ) {
                return array( 'access' => $access, 'secret' => $secret );
            }
        }
    }

    return 'Login succeeded but could not retrieve S3 keys';
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
    if ( ! in_array( $action, array( 'restart', 'stop', 'start', 'save-restart', 'check-reachability', 'generate-vanity', 'import-key-file', 'create-backup', 'restore-backup', 'update' ), true ) ) {
        return;
    }

    // If this is a save-restart, also save config updates first
    if ( $action === 'save-restart' ) {
        $_POST['onionpress_settings_nonce'] = wp_create_nonce( 'onionpress_settings_save' );
        // The settings handler below will fire on the same request
    }

    // Handle backup creation — verify WP password and use it to encrypt the zip
    if ( $action === 'create-backup' ) {
        $bp_pass = $_POST['op_backup_password'] ?? '';
        if ( empty( $bp_pass ) ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Please enter your password.</p></div>';
            } );
            return;
        }
        $current_user = wp_get_current_user();
        if ( ! wp_check_password( $bp_pass, $current_user->user_pass, $current_user->ID ) ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Incorrect password. Please enter the password for your WordPress account.</p></div>';
            } );
            return;
        }
        if ( @file_put_contents( '/var/lib/onionpress/backup-password', $bp_pass ) === false ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Failed to write backup password.</p></div>';
            } );
            return;
        }
    }

    // Handle restore — write password and uploaded zip to shared volume
    if ( $action === 'restore-backup' ) {
        $rp_pass = $_POST['op_restore_password'] ?? '';
        if ( empty( $rp_pass ) ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Please enter the backup password.</p></div>';
            } );
            return;
        }
        if ( empty( $_FILES['op_restore_file'] ) || $_FILES['op_restore_file']['error'] !== UPLOAD_ERR_OK ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Please select a backup zip file to restore.</p></div>';
            } );
            return;
        }
        if ( @file_put_contents( '/var/lib/onionpress/restore-password', $rp_pass ) === false ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Failed to write restore password.</p></div>';
            } );
            return;
        }
        if ( ! move_uploaded_file( $_FILES['op_restore_file']['tmp_name'], '/var/lib/onionpress/restore-upload.zip' ) ) {
            @unlink( '/var/lib/onionpress/restore-password' );
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Failed to save uploaded backup file.</p></div>';
            } );
            return;
        }
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
        'create-backup' => 'create-backup', 'restore-backup' => 'restore-backup',
        'update' => 'update',
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
        'create-backup' => 'Creating backup of',
        'restore-backup' => 'Restoring backup for',
        'update' => 'Updating',
    );
    $label = $label_map[ $action ] ?? 'Processing';
    $causes_downtime = in_array( $action, array( 'restart', 'save-restart', 'stop', 'restore-backup', 'update' ), true );
    $poll_action = $cmd_map[ $action ] ?? $action;
    add_action( 'admin_notices', function () use ( $label, $causes_downtime, $poll_action ) {
        $msg = esc_html( $label ) . ' OnionPress... This may take a minute.';
        if ( $causes_downtime ) {
            $msg .= ' The page will become unavailable during restart.';
        }
        echo '<div class="notice notice-info op-action-notice" data-op-action="' . esc_attr( $poll_action ) . '"><p>' . $msg . '</p></div>';
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

    // Handle Archive.org credentials — convert account/password to S3 keys via xauthn API
    $ia_account  = isset( $_POST['op_ia_account'] )  ? sanitize_text_field( wp_unslash( $_POST['op_ia_account'] ) )  : '';
    $ia_password = isset( $_POST['op_ia_password'] ) ? wp_unslash( $_POST['op_ia_password'] ) : '';
    if ( $ia_account !== '' && $ia_password !== '' ) {
        $s3_keys = onionpress_fetch_ia_s3_keys( $ia_account, $ia_password );
        if ( is_array( $s3_keys ) ) {
            update_option( 'onionpress_archive_s3_access', $s3_keys['access'] );
            update_option( 'onionpress_archive_s3_secret', $s3_keys['secret'] );
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-success"><p>Archive.org credentials saved.</p></div>';
            } );
        } else {
            add_action( 'admin_notices', function () use ( $s3_keys ) {
                echo '<div class="notice notice-error"><p>Archive.org login failed: ' . esc_html( $s3_keys ) . '</p></div>';
            } );
        }
    } elseif ( $ia_account === '' && $ia_password === '' ) {
        // Both empty — clear credentials
        $had_creds = get_option( 'onionpress_archive_s3_access', '' ) !== '';
        if ( $had_creds ) {
            update_option( 'onionpress_archive_s3_access', '' );
            update_option( 'onionpress_archive_s3_secret', '' );
        }
    }

    // Check if a restart is actually needed (must read old values before writing updates)
    $needs_restart = false;
    $restart_keys = array( 'ADDRESS_PREFIX', 'CLOUDFLARE_TUNNEL_TOKEN', 'VM_MEMORY' );
    if ( ! empty( $updates ) ) {
        $old_values = array();
        $config_file = '/var/lib/onionpress/config-current.json';
        if ( file_exists( $config_file ) ) {
            $decoded = json_decode( file_get_contents( $config_file ), true );
            if ( is_array( $decoded ) ) {
                $old_values = $decoded;
            }
        }
        $onion_address = '';
        $sf = '/var/lib/onionpress/status.json';
        if ( file_exists( $sf ) ) {
            $sd = json_decode( file_get_contents( $sf ), true );
            if ( is_array( $sd ) ) {
                $onion_address = $sd['onion_address'] ?? '';
            }
        }
        foreach ( $updates as $key => $val ) {
            if ( ! in_array( $key, $restart_keys, true ) ) {
                continue;
            }
            if ( isset( $old_values[ $key ] ) && $old_values[ $key ] === $val ) {
                continue;
            }
            if ( $key === 'ADDRESS_PREFIX' && $onion_address && strpos( $onion_address, $val ) === 0 ) {
                continue;
            }
            $needs_restart = true;
        }

        // Write config-updates.json for the onionpress script to pick up
        $json = json_encode( $updates, JSON_PRETTY_PRINT );
        $file = '/var/lib/onionpress/config-updates.json';
        if ( @file_put_contents( $file, $json ) === false ) {
            add_action( 'admin_notices', function () {
                echo '<div class="notice notice-error"><p>Failed to write config update. The shared volume may not be mounted.</p></div>';
            } );
            return;
        }

        // Update config-current.json so the form reflects the saved values immediately
        $current = array_merge( $old_values, $updates );
        @file_put_contents( $config_file, json_encode( $current, JSON_PRETTY_PRINT ) );
    }

    // Show success message
    add_action( 'admin_notices', function () use ( $needs_restart ) {
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
        if ( $is_linux && $needs_restart ) {
            $restart_nonce = wp_create_nonce( 'onionpress_action' );
            echo '<div class="notice notice-success is-dismissible"><p>Settings saved. Restart OnionPress for changes to take effect. ';
            echo '<form method="post" style="display:inline;"><input type="hidden" name="onionpress_action_nonce" value="' . esc_attr( $restart_nonce ) . '"><input type="hidden" name="onionpress_action" value="restart"><button type="submit" class="button button-primary" style="margin-left:8px;">Restart Now</button></form>';
            echo '</p></div>';
        } elseif ( ! $is_linux ) {
            echo '<div class="notice notice-success is-dismissible"><p>Settings saved. Changes will take effect within 30 seconds.</p></div>';
        } else {
            echo '<div class="notice notice-success is-dismissible"><p>Settings saved.</p></div>';
        }
    } );
} );

/**
 * Increase upload limit for backup restore on the settings page.
 */
add_filter( 'upload_size_limit', function ( $size ) {
    if ( isset( $_GET['page'] ) && $_GET['page'] === 'onionpress-settings' ) {
        return 512 * 1024 * 1024; // 512 MB
    }
    return $size;
} );

// Set PHP limits when on the settings page (backup uploads can be large)
add_action( 'admin_init', function () {
    if ( isset( $_GET['page'] ) && $_GET['page'] === 'onionpress-settings' ) {
        @ini_set( 'upload_max_filesize', '512M' );
        @ini_set( 'post_max_size', '512M' );
        @ini_set( 'max_execution_time', '600' );
    }
} );

/**
 * AJAX handler for downloading backup files.
 */
add_action( 'wp_ajax_onionpress_download_backup', function () {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized' );
    }
    check_ajax_referer( 'onionpress_download_backup' );

    $filename = sanitize_file_name( $_GET['file'] ?? '' );
    if ( empty( $filename ) ) {
        wp_die( 'No file specified' );
    }

    $filepath = '/var/lib/onionpress/' . $filename;
    if ( ! file_exists( $filepath ) ) {
        wp_die( 'Backup file not found. It may have been cleaned up.' );
    }

    header( 'Content-Type: application/zip' );
    header( 'Content-Disposition: attachment; filename="' . $filename . '"' );
    header( 'Content-Length: ' . filesize( $filepath ) );
    readfile( $filepath );

    // Clean up after download
    @unlink( $filepath );
    @unlink( '/var/lib/onionpress/backup-result.json' );
    exit;
} );

/**
 * AJAX handler for polling action completion.
 */
add_action( 'wp_ajax_onionpress_poll_action', function () {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_send_json_error( 'Unauthorized' );
    }

    $action = sanitize_text_field( $_GET['op_action'] ?? '' );
    $pending = file_exists( '/var/lib/onionpress/requested-action' );

    // Map actions to their result files
    $result_files = array(
        'check-reachability' => '/var/lib/onionpress/reachability-result.json',
        'generate-vanity'    => '/var/lib/onionpress/vanity-result.json',
        'import-key-file'    => '/var/lib/onionpress/import-result.json',
        'create-backup'      => '/var/lib/onionpress/backup-result.json',
        'restore-backup'     => '/var/lib/onionpress/restore-result.json',
        'update'             => '/var/lib/onionpress/update-result.json',
    );

    if ( isset( $result_files[ $action ] ) ) {
        $rf = $result_files[ $action ];
        if ( file_exists( $rf ) ) {
            $result = json_decode( file_get_contents( $rf ), true );
            wp_send_json( array( 'done' => true, 'result' => $result ) );
        }
        wp_send_json( array( 'done' => false ) );
    }

    // For start/stop/restart — done when action file is consumed
    wp_send_json( array( 'done' => ! $pending ) );
} );

/**
 * AJAX handler for checking latest version (cached 10 min).
 */
add_action( 'wp_ajax_onionpress_check_update', function () {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_send_json_error( 'Unauthorized' );
    }

    $current = trim( @file_get_contents( '/var/lib/onionpress/version' ) ?: 'unknown' );

    // Check transient cache first
    $cached = get_transient( 'onionpress_latest_release' );
    if ( $cached !== false ) {
        wp_send_json( array(
            'current'   => $current,
            'latest'    => $cached['tag'],
            'update'    => version_compare( ltrim( $cached['tag'], 'v' ), ltrim( $current, 'v' ), '>' ),
        ) );
    }

    // Fetch from GitHub releases API
    $resp = wp_remote_get( 'https://api.github.com/repos/brewsterkahle/onionpress/releases/latest', array(
        'timeout' => 10,
        'headers' => array( 'User-Agent' => 'OnionPress (+https://github.com/brewsterkahle/onionpress)' ),
    ) );
    if ( is_wp_error( $resp ) || wp_remote_retrieve_response_code( $resp ) !== 200 ) {
        wp_send_json( array( 'current' => $current, 'latest' => null, 'update' => false, 'error' => 'Failed to check for updates' ) );
    }

    $data = json_decode( wp_remote_retrieve_body( $resp ), true );
    $tag  = $data['tag_name'] ?? '';

    set_transient( 'onionpress_latest_release', array( 'tag' => $tag ), 600 ); // 10 min

    wp_send_json( array(
        'current'   => $current,
        'latest'    => $tag,
        'update'    => version_compare( ltrim( $tag, 'v' ), ltrim( $current, 'v' ), '>' ),
    ) );
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

                <!-- Archive.org Credentials -->
                <tr>
                    <th scope="row"><label for="op_ia_account">Archive.org Account</label></th>
                    <td>
                        <input type="text" name="op_ia_account" id="op_ia_account"
                               value="" class="regular-text" autocomplete="off"
                               placeholder="<?php echo $s3_access ? '(credentials saved)' : ''; ?>">
                        <p class="description">Used to archive your site on the Wayback Machine, making it more robust and permanent. <?php if ( $s3_access ) echo '<strong>Credentials are configured.</strong> Re-enter to update.'; ?></p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="op_ia_password">Archive.org Password</label></th>
                    <td>
                        <span class="op-password-wrap">
                            <input type="password" name="op_ia_password" id="op_ia_password"
                                   value="" class="regular-text" autocomplete="off">
                            <button type="button" class="op-eye-toggle" aria-label="Show password">&#128065;</button>
                        </span>
                        <p class="description">Your Archive.org password. Used to retrieve API keys — not stored.</p>
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
        <hr style="border: none; border-top: 3px solid #c3c4c7; margin: 30px 0;">
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

        <!-- Update -->
        <hr>
        <h2>Updates</h2>
        <div id="op-update-status"><p class="description">Checking for updates...</p></div>

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

        <!-- Backup -->
        <hr>
        <h2>Backup</h2>
        <?php
        $backup_file = '/var/lib/onionpress/backup-result.json';
        if ( file_exists( $backup_file ) ) {
            $backup_result = json_decode( file_get_contents( $backup_file ), true );
            if ( is_array( $backup_result ) ) {
                if ( ! empty( $backup_result['success'] ) ) {
                    $dl_filename = $backup_result['filename'] ?? '';
                    echo '<p><strong style="color:#16a34a">Backup created:</strong> <code>' . esc_html( $dl_filename ) . '</code>';
                    if ( $dl_filename ) {
                        $dl_url = admin_url( 'admin-ajax.php?action=onionpress_download_backup&file=' . urlencode( $dl_filename ) . '&_wpnonce=' . wp_create_nonce( 'onionpress_download_backup' ) );
                        echo ' &mdash; <a href="' . esc_url( $dl_url ) . '">Download</a>';
                    }
                    echo '</p>';
                } elseif ( ! empty( $backup_result['error'] ) ) {
                    echo '<p><strong style="color:#dc2626">Error:</strong> ' . esc_html( $backup_result['error'] ) . '</p>';
                }
            }
        }
        ?>
        <p class="description">Create a password-protected backup of your database, wp-content, Tor keys, and config. Your WordPress password encrypts the backup — you'll need it to restore.</p>
        <form method="post" style="margin-top: 8px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="create-backup">
            <table class="form-table" role="presentation">
                <tr>
                    <th scope="row"><label for="op_backup_password">Password for <?php echo esc_html( wp_get_current_user()->user_login ); ?></label></th>
                    <td>
                        <span class="op-password-wrap">
                            <input type="password" name="op_backup_password" id="op_backup_password" class="regular-text" required autocomplete="current-password">
                            <button type="button" class="op-eye-toggle" aria-label="Show password">&#128065;</button>
                        </span>
                        <p class="description">Enter your WordPress password to create the backup.</p>
                    </td>
                </tr>
            </table>
            <?php submit_button( 'Create Backup', 'secondary', 'submit', false ); ?>
        </form>

        <!-- Restore -->
        <hr>
        <h2>Restore</h2>
        <?php
        $restore_file = '/var/lib/onionpress/restore-result.json';
        if ( file_exists( $restore_file ) ) {
            $restore_result = json_decode( file_get_contents( $restore_file ), true );
            if ( is_array( $restore_result ) ) {
                if ( ! empty( $restore_result['success'] ) ) {
                    echo '<p><strong style="color:#16a34a">Restored:</strong> <code>' . esc_html( $restore_result['address'] ?? '' ) . '</code></p>';
                } elseif ( ! empty( $restore_result['error'] ) ) {
                    echo '<p><strong style="color:#dc2626">Error:</strong> ' . esc_html( $restore_result['error'] ) . '</p>';
                }
            }
        }
        ?>
        <p class="description">Restore from an OnionPress backup zip. This will <strong>overwrite</strong> your current database, wp-content, and Tor keys. OnionPress will restart automatically after restore.</p>
        <form method="post" enctype="multipart/form-data" style="margin-top: 8px;">
            <?php wp_nonce_field( 'onionpress_action', 'onionpress_action_nonce' ); ?>
            <input type="hidden" name="onionpress_action" value="restore-backup">
            <table class="form-table" role="presentation">
                <tr>
                    <th scope="row"><label for="op_restore_file">Backup File</label></th>
                    <td>
                        <input type="file" name="op_restore_file" id="op_restore_file" accept=".zip" required>
                        <p class="description">Upload your OnionPress backup .zip file.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="op_restore_password">Backup Password</label></th>
                    <td>
                        <span class="op-password-wrap">
                            <input type="password" name="op_restore_password" id="op_restore_password" class="regular-text" required autocomplete="current-password">
                            <button type="button" class="op-eye-toggle" aria-label="Show password">&#128065;</button>
                        </span>
                        <p class="description">The WordPress password of the admin who created the backup.</p>
                    </td>
                </tr>
            </table>
            <?php submit_button( 'Restore from Backup', 'secondary', 'submit', false ); ?>
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
        <p class="description">Generate a new vanity .onion address matching your Address Prefix setting. This will <strong>replace</strong> your current address, forever. Not reversible. Generation can take seconds to minutes depending on prefix length.</p>
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
        <p class="description">Import a pre-generated ed25519 key. This will <strong>replace</strong> your current onion address, forever. Not reversible. Paste the base32-encoded key or upload the <code>hs_ed25519_secret_key</code> file.</p>
        <form method="post" enctype="multipart/form-data" style="margin-top: 8px;" id="op-import-key-form">
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
    <style>
        .op-password-wrap { display: inline-flex; align-items: center; }
        .op-password-wrap input { margin-right: 0; }
        .op-eye-toggle { background: none; border: 1px solid #8c8f94; border-left: 0; border-radius: 0 4px 4px 0; padding: 0 8px; cursor: pointer; font-size: 16px; line-height: 30px; height: 30px; color: #50575e; }
        .op-eye-toggle:hover { color: #135e96; }
        .op-password-wrap input.regular-text { border-radius: 4px 0 0 4px; }
    </style>
    <script>
        /* Client-side validation: highlight empty required fields */
        document.querySelectorAll('form').forEach(function(form) {
            form.addEventListener('submit', function(e) {
                var missing = false;
                form.querySelectorAll('input[required]').forEach(function(input) {
                    var label = form.querySelector('label[for="' + input.id + '"]');
                    if (!input.value && input.type !== 'file' || input.type === 'file' && !input.files.length) {
                        if (label) label.style.color = '#dc2626';
                        input.style.borderColor = '#dc2626';
                        missing = true;
                    } else {
                        if (label) label.style.color = '';
                        input.style.borderColor = '';
                    }
                });
                if (missing) e.preventDefault();
            });
            form.querySelectorAll('input[required]').forEach(function(input) {
                input.addEventListener('input', function() {
                    var label = form.querySelector('label[for="' + input.id + '"]');
                    if (label) label.style.color = '';
                    input.style.borderColor = '';
                });
                if (input.type === 'file') {
                    input.addEventListener('change', function() {
                        var label = form.querySelector('label[for="' + input.id + '"]');
                        if (label) label.style.color = '';
                        input.style.borderColor = '';
                    });
                }
            });
        });

        /* Import key form: require at least one of base32 or file */
        var ikForm = document.getElementById('op-import-key-form');
        if (ikForm) {
            ikForm.addEventListener('submit', function(e) {
                var b32 = ikForm.querySelector('#op_key_b32');
                var file = ikForm.querySelector('#op_key_file');
                var b32Label = ikForm.querySelector('label[for="op_key_b32"]');
                var fileLabel = ikForm.querySelector('label[for="op_key_file"]');
                if (!b32.value && !file.files.length) {
                    if (b32Label) b32Label.style.color = '#dc2626';
                    if (fileLabel) fileLabel.style.color = '#dc2626';
                    b32.style.borderColor = '#dc2626';
                    file.style.borderColor = '#dc2626';
                    e.preventDefault();
                } else {
                    if (b32Label) b32Label.style.color = '';
                    if (fileLabel) fileLabel.style.color = '';
                    b32.style.borderColor = '';
                    file.style.borderColor = '';
                }
            });
            ['op_key_b32', 'op_key_file'].forEach(function(id) {
                var el = document.getElementById(id);
                if (el) el.addEventListener(el.type === 'file' ? 'change' : 'input', function() {
                    var label = ikForm.querySelector('label[for="' + id + '"]');
                    if (label) label.style.color = '';
                    el.style.borderColor = '';
                });
            });
        }

        document.querySelectorAll('.op-eye-toggle').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var input = this.previousElementSibling;
                if (input.type === 'password') {
                    input.type = 'text';
                    this.setAttribute('aria-label', 'Hide password');
                } else {
                    input.type = 'password';
                    this.setAttribute('aria-label', 'Show password');
                }
            });
        });

        /* Poll for action completion and update the notice */
        (function() {
            var notice = document.querySelector('.op-action-notice');
            if (!notice) return;
            var action = notice.getAttribute('data-op-action');
            if (!action) return;

            var poll = setInterval(function() {
                fetch(ajaxurl + '?action=onionpress_poll_action&op_action=' + encodeURIComponent(action))
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (!data.done) return;
                        clearInterval(poll);

                        var p = notice.querySelector('p');
                        var result = data.result;

                        if (result && result.success === true) {
                            notice.className = 'notice notice-success is-dismissible';
                            var msg = 'Done!';
                            if (result.address) msg = 'Done! Address: ' + result.address;
                            if (result.filename) msg = 'Backup ready: ' + result.filename;
                            if (result.reachable === true) msg = 'Onion service is reachable (HTTP ' + (result.http_code || '200') + ')';
                            if (result.reachable === false) msg = 'Onion service is not reachable' + (result.error ? ': ' + result.error : '');
                            p.textContent = msg;
                        } else if (result && result.error) {
                            notice.className = 'notice notice-error is-dismissible';
                            p.textContent = 'Error: ' + result.error;
                        } else {
                            notice.className = 'notice notice-success is-dismissible';
                            p.textContent = 'Done!';
                            /* Reload to show updated state */
                            setTimeout(function() { location.reload(); }, 1500);
                        }

                        /* Add download link for backup */
                        if (result && result.success && result.filename) {
                            var link = document.createElement('a');
                            link.href = ajaxurl + '?action=onionpress_download_backup&file=' + encodeURIComponent(result.filename) + '&_wpnonce=<?php echo wp_create_nonce( "onionpress_download_backup" ); ?>';
                            link.textContent = ' Download';
                            link.style.marginLeft = '8px';
                            p.appendChild(link);
                        }
                    })
                    .catch(function() { /* server may be restarting — keep polling */ });
            }, 3000);
        })();

        /* Check for updates (async, doesn't stall page) */
        (function() {
            var el = document.getElementById('op-update-status');
            if (!el) return;
            fetch(ajaxurl + '?action=onionpress_check_update')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    var current = data.current || 'unknown';
                    if (data.update && data.latest) {
                        el.innerHTML = '<form method="post" style="display:inline;">' +
                            '<input type="hidden" name="onionpress_action_nonce" value="<?php echo wp_create_nonce( "onionpress_action" ); ?>">' +
                            '<input type="hidden" name="onionpress_action" value="update">' +
                            '<p><strong>Update available:</strong> ' + data.latest + ' (you are on ' + current + ')</p>' +
                            '<button type="submit" class="button button-primary">Update to ' + data.latest + '</button>' +
                            '</form>';
                    } else if (data.latest) {
                        el.innerHTML = '<p class="description">You are on the latest version (' + current + ').</p>';
                    } else {
                        el.innerHTML = '<p class="description">Version ' + current + '. ' + (data.error || 'Could not check for updates.') + '</p>';
                    }
                })
                .catch(function() {
                    el.innerHTML = '<p class="description">Could not check for updates.</p>';
                });
        })();
    </script>
    <?php
}
