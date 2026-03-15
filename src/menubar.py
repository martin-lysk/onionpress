#!/usr/bin/env python3
"""
onionpress Menu Bar Application
Provides a simple menu bar interface to control the WordPress + Tor onion service
"""

import rumps
import subprocess
import os
import threading
import time
import json
import plistlib
import sys
from datetime import datetime
import AppKit
import signal
import socket
import atexit
import re

# Add scripts directory to path for imports
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir)

import key_manager
import backup_manager
import onion_proxy
import install_native_messaging
import onionheaven


class _HelpButtonTarget(AppKit.NSObject):
    """ObjC target for (?) help buttons in the settings dialog."""
    _help_texts = {}
    _icon_path = None

    def helpClicked_(self, sender):
        text = self._help_texts.get(sender.tag(), "")
        if text:
            a = AppKit.NSAlert.alloc().init()
            a.setMessageText_("Help")
            a.setInformativeText_(text)
            if self._icon_path and os.path.exists(self._icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(self._icon_path)
                if icon:
                    a.setIcon_(icon)
            a.runModal()


def parse_version(version_str):
    """Parse a version string like '2.10.3' into a tuple of ints for comparison."""
    try:
        return tuple(int(x) for x in version_str.split('.'))
    except (ValueError, AttributeError):
        return (0,)


def _main_thread(func):
    """Run func on the main thread (required for AppKit UI updates)."""
    AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(func)


class _BackupProgressWindow:
    """A small floating window that shows backup/restore progress."""

    def __init__(self, title):
        self._title = title
        self._window = None
        self._status_field = None

    def show(self):
        w = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, 380, 120),
            AppKit.NSWindowStyleMaskTitled,
            AppKit.NSBackingStoreBuffered,
            False
        )
        w.setTitle_(self._title)
        w.setLevel_(AppKit.NSFloatingWindowLevel)
        w.center()
        w.setReleasedWhenClosed_(False)
        w.setHidesOnDeactivate_(False)

        content = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 380, 120))

        # Spinner
        spinner = AppKit.NSProgressIndicator.alloc().initWithFrame_(
            AppKit.NSMakeRect(20, 75, 24, 24))
        spinner.setStyle_(1)  # NSProgressIndicatorStyleSpinning
        spinner.startAnimation_(None)
        content.addSubview_(spinner)
        self._spinner = spinner

        # Title label
        title_label = AppKit.NSTextField.labelWithString_(self._title + "...")
        title_label.setFrame_(AppKit.NSMakeRect(52, 77, 300, 20))
        title_label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(14))
        content.addSubview_(title_label)

        # Status text
        status = AppKit.NSTextField.labelWithString_("Starting...")
        status.setFrame_(AppKit.NSMakeRect(20, 20, 340, 45))
        status.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        status.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        status.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        content.addSubview_(status)
        self._status_field = status

        w.setContentView_(content)
        w.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self._window = w

    def update(self, message):
        if self._status_field:
            self._status_field.setStringValue_(message)

    def finish(self, message):
        # Close the progress window and show a simple alert
        if self._window:
            self._window.orderOut_(None)
            self._window = None
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Done")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.runModal()


class _LogViewerActions(AppKit.NSObject):
    """Singleton handling custom log viewer menu actions."""

    _shared = None

    @classmethod
    def shared(cls):
        if cls._shared is None:
            cls._shared = cls.alloc().init()
        return cls._shared

    @staticmethod
    def _active_viewer():
        key_win = AppKit.NSApp.keyWindow()
        if not key_win:
            return None
        for inst in _LogViewerWindow._instances.values():
            if inst._window is key_win:
                return inst
        return None

    def clearLog_(self, sender):
        viewer = self._active_viewer()
        if viewer:
            try:
                open(viewer._file_path, 'w').close()
                viewer._text_view.textStorage().mutableString().setString_("")
                viewer._offset = 0
            except Exception:
                pass

    def toggleWordWrap_(self, sender):
        viewer = self._active_viewer()
        if not viewer:
            return
        tv = viewer._text_view
        container = tv.textContainer()
        scroll = tv.enclosingScrollView()
        if container.widthTracksTextView():
            # Disable word wrap — enable horizontal scrolling
            container.setWidthTracksTextView_(False)
            container.setContainerSize_(AppKit.NSMakeSize(1e7, 1e7))
            tv.setHorizontallyResizable_(True)
            if scroll:
                scroll.setHasHorizontalScroller_(True)
        else:
            # Enable word wrap
            if scroll:
                scroll.setHasHorizontalScroller_(False)
                width = scroll.contentView().bounds().size.width
            else:
                width = tv.frame().size.width
            container.setContainerSize_(AppKit.NSMakeSize(width, 1e7))
            container.setWidthTracksTextView_(True)
            tv.setHorizontallyResizable_(False)

    def increaseFontSize_(self, sender):
        viewer = self._active_viewer()
        if viewer:
            font = viewer._text_view.font()
            new_size = min(font.pointSize() + 2, 36)
            viewer._text_view.setFont_(
                AppKit.NSFont.fontWithName_size_(font.fontName(), new_size))

    def decreaseFontSize_(self, sender):
        viewer = self._active_viewer()
        if viewer:
            font = viewer._text_view.font()
            new_size = max(font.pointSize() - 2, 8)
            viewer._text_view.setFont_(
                AppKit.NSFont.fontWithName_size_(font.fontName(), new_size))


class _LogViewerWindow:
    """A read-only log viewer window with live tailing."""

    _instances = {}  # file_path -> instance (singleton per file)

    @classmethod
    def show_for_file(cls, file_path, title):
        """Show (or refocus) a log viewer for the given file."""
        existing = cls._instances.get(file_path)
        if existing and existing._window and existing._window.isVisible():
            existing._window.makeKeyAndOrderFront_(None)
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            return existing
        inst = cls(file_path, title)
        cls._instances[file_path] = inst
        inst._show()
        return inst

    @classmethod
    def close_all(cls):
        """Close all open log viewer windows and stop their polling threads."""
        for inst in list(cls._instances.values()):
            inst._stop()
        cls._instances.clear()

    def __init__(self, file_path, title):
        self._file_path = file_path
        self._title = title
        self._window = None
        self._text_view = None
        self._offset = 0
        self._running = False

    def _show(self):
        style = (AppKit.NSWindowStyleMaskTitled
                 | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskResizable
                 | AppKit.NSWindowStyleMaskMiniaturizable)
        w = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, 720, 480), style,
            AppKit.NSBackingStoreBuffered, False)
        w.setTitle_(self._title)
        w.setLevel_(AppKit.NSNormalWindowLevel)
        w.center()
        w.setReleasedWhenClosed_(False)
        w.setHidesOnDeactivate_(False)

        # Scroll view fills the window
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 720, 480))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)

        # Text view
        tv = AppKit.NSTextView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 720, 480))
        tv.setEditable_(False)
        tv.setSelectable_(True)
        self._log_font = AppKit.NSFont.fontWithName_size_("Menlo", 12)
        self._log_text_color = AppKit.NSColor.textColor()
        tv.setFont_(self._log_font)
        tv.setTextColor_(self._log_text_color)
        tv.setBackgroundColor_(AppKit.NSColor.textBackgroundColor())
        tv.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        # Allow horizontal scrolling for long lines
        tv.setHorizontallyResizable_(False)
        tv.textContainer().setWidthTracksTextView_(True)
        tv.setUsesFindBar_(True)
        tv.setIncrementalSearchingEnabled_(True)

        scroll.setDocumentView_(tv)
        w.setContentView_(scroll)

        self._window = w
        self._text_view = tv

        # Load initial content (last 500 lines)
        self._load_initial()

        # Ensure the app has an Edit menu so Cmd+C/A/V work in the text view.
        # LSUIElement apps have no menu bar by default, so standard key
        # equivalents are not wired up without this.
        self._ensure_edit_menu()

        w.makeKeyAndOrderFront_(None)
        w.makeFirstResponder_(tv)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

        # Start polling thread
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    def _load_initial(self):
        """Read last 500 lines of the file and display them."""
        try:
            if not os.path.exists(self._file_path):
                self._offset = 0
                return
            with open(self._file_path, 'r', encoding='utf-8', errors='replace') as f:
                # Seek backwards to find last 500 lines
                f.seek(0, 2)
                file_size = f.tell()
                if file_size == 0:
                    self._offset = 0
                    return
                # Read in chunks from the end to find 500 newlines
                chunk_size = 8192
                lines_found = 0
                pos = file_size
                while pos > 0 and lines_found < 500:
                    read_size = min(chunk_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    lines_found += chunk.count('\n')
                # Now read from pos to end
                f.seek(pos)
                content = f.read()
                # If we overshot, trim to last 500 lines
                if lines_found > 500:
                    lines = content.split('\n')
                    content = '\n'.join(lines[-(500 + 1):])
                self._offset = file_size
            if content:
                self._append_attributed(content)
                # Scroll to bottom
                end = self._text_view.textStorage().length()
                self._text_view.scrollRangeToVisible_(AppKit.NSMakeRange(end, 0))
        except Exception:
            self._offset = 0

    def _is_near_bottom(self):
        """Check if the scroll position is near the bottom."""
        scroll_view = self._text_view.enclosingScrollView()
        if not scroll_view:
            return True
        clip = scroll_view.contentView()
        doc_height = self._text_view.frame().size.height
        clip_height = clip.bounds().size.height
        scroll_y = clip.bounds().origin.y
        # "Near bottom" = within 50 points of the end
        return (scroll_y + clip_height) >= (doc_height - 50)

    def _append_attributed(self, text):
        """Append text with correct font and color (respects dark mode)."""
        attrs = {
            AppKit.NSFontAttributeName: self._log_font,
            AppKit.NSForegroundColorAttributeName: self._log_text_color,
        }
        astr = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        self._text_view.textStorage().appendAttributedString_(astr)

    def _poll_loop(self):
        """Background thread: poll file for new content every 1.5s."""
        while self._running:
            time.sleep(1.5)
            # Check if window is still visible
            if not self._running:
                break
            try:
                visible = self._window and self._window.isVisible()
            except Exception:
                visible = False
            if not visible:
                self._running = False
                # Remove from instances
                _LogViewerWindow._instances.pop(self._file_path, None)
                break
            try:
                if not os.path.exists(self._file_path):
                    continue
                file_size = os.path.getsize(self._file_path)
                if file_size < self._offset:
                    # File was truncated — reload
                    self._offset = 0
                    def reload():
                        storage = self._text_view.textStorage()
                        storage.deleteCharactersInRange_(AppKit.NSMakeRange(0, storage.length()))
                        self._load_initial()
                    _main_thread(reload)
                    continue
                if file_size == self._offset:
                    continue
                # Read new content
                with open(self._file_path, 'r', encoding='utf-8',
                           errors='replace') as f:
                    f.seek(self._offset)
                    new_content = f.read()
                self._offset = file_size
                if new_content:
                    def append(text=new_content):
                        was_near = self._is_near_bottom()
                        self._append_attributed(text)
                        if was_near:
                            end = self._text_view.textStorage().length()
                            self._text_view.scrollRangeToVisible_(
                                AppKit.NSMakeRange(end, 0))
                    _main_thread(append)
            except Exception:
                pass

    @staticmethod
    def _ensure_edit_menu():
        """Add Edit and View menus so standard key equivalents work."""
        main_menu = AppKit.NSApp.mainMenu()
        if not main_menu:
            main_menu = AppKit.NSMenu.alloc().init()
            AppKit.NSApp.setMainMenu_(main_menu)
        # Check if menus already exist
        for i in range(main_menu.numberOfItems()):
            if main_menu.itemAtIndex_(i).title() == "Edit":
                return

        actions = _LogViewerActions.shared()

        # Edit menu
        edit_menu = AppKit.NSMenu.alloc().initWithTitle_("Edit")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a")
        edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        find_item = edit_menu.addItemWithTitle_action_keyEquivalent_("Find\u2026", "performFindPanelAction:", "f")
        find_item.setTag_(1)  # NSFindPanelActionShowFindPanel
        find_next = edit_menu.addItemWithTitle_action_keyEquivalent_("Find Next", "performFindPanelAction:", "g")
        find_next.setTag_(2)  # NSFindPanelActionNext
        find_prev = edit_menu.addItemWithTitle_action_keyEquivalent_("Find Previous", "performFindPanelAction:", "G")
        find_prev.setTag_(3)  # NSFindPanelActionPrevious
        edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        clear_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Clear Log", "clearLog:", "k")
        clear_item.setTarget_(actions)
        edit_menu.addItem_(clear_item)
        edit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Edit", None, "")
        edit_item.setSubmenu_(edit_menu)
        main_menu.addItem_(edit_item)

        # View menu
        view_menu = AppKit.NSMenu.alloc().initWithTitle_("View")
        wrap_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Toggle Word Wrap", "toggleWordWrap:", "")
        wrap_item.setTarget_(actions)
        view_menu.addItem_(wrap_item)
        view_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        bigger = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Bigger", "increaseFontSize:", "=")
        bigger.setTarget_(actions)
        view_menu.addItem_(bigger)
        smaller = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Smaller", "decreaseFontSize:", "-")
        smaller.setTarget_(actions)
        view_menu.addItem_(smaller)
        view_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "View", None, "")
        view_item.setSubmenu_(view_menu)
        main_menu.addItem_(view_item)

    def _stop(self):
        """Stop polling and close the window."""
        self._running = False
        if self._window:
            try:
                self._window.orderOut_(None)
            except Exception:
                pass


class OnionPressApp(rumps.App):
    def __init__(self):
        # Get paths first (fast - no I/O)
        self.app_support = os.path.expanduser("~/.onionpress")
        self.script_dir = os.path.dirname(os.path.realpath(__file__))

        # Single-instance safety net via PID file
        self.pid_file = os.path.join(self.app_support, "menubar.pid")
        os.makedirs(self.app_support, exist_ok=True)
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file) as f:
                    old_pid = int(f.read().strip())
                # Check if that PID is still alive
                os.kill(old_pid, 0)
                # Process is alive — signal reopen and exit
                reopen_file = os.path.join(self.app_support, ".reopen")
                with open(reopen_file, 'w') as f:
                    f.write(str(os.getpid()))
                sys.exit(0)
            except (ProcessLookupError, ValueError, OSError):
                # Stale PID file — continue launching
                pass
        # Write our PID
        with open(self.pid_file, 'w') as f:
            f.write(str(os.getpid()))
        # Register cleanup for normal exit
        atexit.register(self._remove_pid_file)
        # Register signal handlers for clean removal on SIGTERM/SIGINT
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # When running as py2app bundle, __file__ is in Contents/Resources/
        # so we need to use that as resources_dir, not the parent
        if getattr(sys, 'frozen', False):
            # Running as py2app bundle
            # __file__ is like: .../MenubarApp/Contents/Resources/menubar.py (in zip)
            # MenubarApp is nested inside OnionPress.app
            # Structure: OnionPress.app/Contents/Resources/MenubarApp/Contents/Resources/menubar.py
            menubar_resources_dir = os.path.join(os.environ.get('RESOURCEPATH', ''))
            if not menubar_resources_dir:
                # Fallback: get from bundle structure
                bundle_contents = os.path.dirname(os.path.dirname(self.script_dir))
                menubar_resources_dir = os.path.join(bundle_contents, 'Resources')

            # Keep menubar resources for icons
            self.resources_dir = menubar_resources_dir

            # Navigate to parent OnionPress.app bundle for launcher script and bin dir
            # MenubarApp/Contents/Resources -> MenubarApp/Contents -> MenubarApp -> OnionPress.app/Resources -> OnionPress.app/Contents
            menubar_contents = os.path.dirname(menubar_resources_dir)  # MenubarApp/Contents
            menubar_app = os.path.dirname(menubar_contents)  # MenubarApp
            parent_resources = os.path.dirname(menubar_app)  # OnionPress.app/Contents/Resources
            self.parent_resources_dir = parent_resources  # Store for accessing docker/ and other parent resources
            self.contents_dir = os.path.dirname(parent_resources)  # OnionPress.app/Contents
            self.macos_dir = os.path.join(self.contents_dir, "MacOS")
            self.launcher_script = os.path.join(self.macos_dir, "onionpress")
            self.bin_dir = os.path.join(parent_resources, "bin")
        else:
            # Running as regular Python script
            self.resources_dir = os.path.dirname(self.script_dir)
            self.parent_resources_dir = self.resources_dir  # Same as resources_dir when not bundled
            self.contents_dir = os.path.dirname(self.resources_dir)
            self.macos_dir = os.path.join(self.contents_dir, "MacOS")
            self.launcher_script = os.path.join(self.macos_dir, "onionpress")
            self.bin_dir = os.path.join(self.resources_dir, "bin")
        self.colima_home = os.path.join(self.app_support, "colima")
        self.info_plist = os.path.join(self.contents_dir, "Info.plist")
        self.log_file = os.path.join(self.app_support, "onionpress.log")

        # Initialize rumps WITHOUT icon first (fastest possible)
        super(OnionPressApp, self).__init__("", quit_button=None, template=False)

        # Show launch splash IMMEDIATELY before any I/O
        self.launch_splash = None
        self.show_launch_splash()

        # Now load icon files (this does I/O but splash is already showing)
        self.icon_running = os.path.join(self.resources_dir, "menubar-icon-running.png")
        self.icon_stopped = os.path.join(self.resources_dir, "menubar-icon-stopped.png")
        self.icon_starting = os.path.join(self.resources_dir, "menubar-icon-starting.png")

        # Set the stopped icon
        self.icon = self.icon_stopped

        # Set version to placeholder (will be updated in background)
        self.version = "2.4.32"

        # Set up environment variables (fast - no I/O)
        docker_config_dir = os.path.join(self.app_support, "docker-config")
        os.environ["PATH"] = f"{self.bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["COLIMA_HOME"] = self.colima_home
        os.environ["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
        os.environ["LIMA_INSTANCE"] = "onionpress"
        os.environ["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        os.environ["DOCKER_CONFIG"] = docker_config_dir

        # Detect port offset for multi-user support.
        # Try to bind the WordPress port; if in use (by another user's instance),
        # bump offset by 10000 until a free port is found.
        port_offset = 0
        while True:
            test_port = 8080 + port_offset
            if test_port > 65535:
                port_offset = 0  # fall back to default
                break
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(('127.0.0.1', test_port))
                s.close()
                break
            except OSError:
                port_offset += 10000
        self.wp_port = 8080 + port_offset
        self.socks_port = 9050 + port_offset
        self.proxy_port = 9077 + port_offset
        os.environ["ONIONPRESS_PORT_OFFSET"] = str(port_offset)
        os.environ["ONIONPRESS_WP_PORT"] = str(self.wp_port)
        os.environ["ONIONPRESS_SOCKS_PORT"] = str(self.socks_port)
        os.environ["ONIONPRESS_PROXY_PORT"] = str(self.proxy_port)
        # Update onion_proxy module globals (already imported with defaults)
        onion_proxy.PROXY_PORT = self.proxy_port
        onion_proxy.PHP_PROXY_PORT = self.wp_port

        # Update OnionHeaven hub address from config
        oh_addr = self._read_config_value(
            "ONIONHEAVEN_ADDRESS",
            "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion")
        if oh_addr:
            onionheaven.ONIONHEAVEN_ADDRESS = oh_addr

        # Do slow I/O operations in background after icon appears
        def background_init():
            # Append to existing log file (continuous log across sessions)
            with open(self.log_file, 'a') as f:
                f.write(f"\n{'=' * 60}\n")
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] === New session starting ===\n")
                f.write(f"{'=' * 60}\n")

            # Debug logging
            with open(self.log_file, 'a') as f:
                f.write(f"DEBUG: frozen={getattr(sys, 'frozen', False)}\n")
                f.write(f"DEBUG: resources_dir={self.resources_dir}\n")
                f.write(f"DEBUG: bin_dir={self.bin_dir}\n")
                f.write(f"DEBUG: launcher_script={self.launcher_script}\n")
                f.write(f"DEBUG: icon_stopped exists={os.path.exists(self.icon_stopped)}\n")
                f.write(f"DEBUG: icon_stopped path={self.icon_stopped}\n")
                f.write(f"DEBUG: rumps initialized successfully\n")

            # Create Docker config without credential store (avoids docker-credential-osxkeychain errors)
            os.makedirs(docker_config_dir, exist_ok=True)
            docker_config_file = os.path.join(docker_config_dir, "config.json")
            if not os.path.exists(docker_config_file):
                with open(docker_config_file, 'w') as f:
                    f.write('{\n\t"auths": {},\n\t"currentContext": "colima"\n}\n')

            # Install docker-compose plugin: prefer bundled, fall back to system
            cli_plugins_dir = os.path.join(docker_config_dir, "cli-plugins")
            os.makedirs(cli_plugins_dir, exist_ok=True)
            compose_plugin_dest = os.path.join(cli_plugins_dir, "docker-compose")
            bundled_compose = os.path.join(self.bin_dir, "docker-compose")
            system_compose = os.path.expanduser("~/.docker/cli-plugins/docker-compose")
            if os.path.isfile(bundled_compose) and not os.path.exists(compose_plugin_dest):
                try:
                    os.symlink(bundled_compose, compose_plugin_dest)
                except Exception:
                    pass
            elif os.path.islink(system_compose) and not os.path.exists(compose_plugin_dest):
                try:
                    os.symlink(system_compose, compose_plugin_dest)
                except Exception:
                    pass

            # Get actual version from Info.plist
            self.version = self.get_version()

            # Log version information at startup
            self.log_version_info()

            # Update browser menu title after checking filesystem
            self.update_browser_menu_title()

            # Install native messaging manifests for browser extension support
            try:
                install_native_messaging.install(log_func=self.log)
            except Exception as e:
                self.log(f"Native messaging install failed: {e}")

            # Sync login item LaunchAgent with config
            launch_on_login = self._read_config_value("LAUNCH_ON_LOGIN", "yes")
            if launch_on_login == "yes" and not self._is_login_item_installed():
                self.add_login_item()
            elif launch_on_login != "yes" and self._is_login_item_installed():
                self.remove_login_item()

            # Check if Cloudflare Tunnel is configured
            cf_token = self._read_config_value("CLOUDFLARE_TUNNEL_TOKEN")
            if cf_token:
                self.cloudflare_tunnel_enabled = True
                self.log("Cloudflare Tunnel configured")

        # Start background initialization
        threading.Thread(target=background_init, daemon=True).start()

        # State — load cached onion address from previous run if available
        cached_addr_file = os.path.join(self.app_support, "onion_address")
        try:
            with open(cached_addr_file) as f:
                cached = f.read().strip()
            if cached and cached.endswith('.onion'):
                self.onion_address = cached
            else:
                self.onion_address = "Starting..."
        except (OSError, IOError):
            self.onion_address = "Starting..."
        self.is_running = False
        self.is_ready = False  # WordPress is ready to serve requests
        self.checking = False
        self._checking_lock = threading.Lock()  # Protect self.checking from race conditions
        self.web_log_process = None  # Background process for web logs
        self.web_log_file_handle = None  # File handle for web log capture
        self.last_status_logged = None  # Track last logged status to avoid spam
        self.auto_opened_browser = False  # Track if we've auto-opened browser this session
        self.setup_dialog_showing = False  # Track if setup dialog is currently showing
        self.setup_alert = None  # Reference to NSAlert for programmatic dismissal
        self.monitoring_tor_install = False  # Track if we're monitoring for Tor Browser installation
        self.caffeinate_process = None  # Process handle for caffeinate to prevent sleep
        self.proxy_server = None  # Onion proxy HTTP server instance
        self.proxy_thread = None  # Thread running the proxy server
        self._wp_installed = None  # None = unknown, True/False = checked
        self._wp_not_installed_count = 0  # Consecutive "not installed" results
        self._setup_page_opened = False  # Track if we've opened the setup page
        self._port_conflict = False  # True if ports are in use by another instance
        self._ports_checked = False  # True after port conflict check completes
        self._has_internet = True          # Host-level internet connectivity
        self._last_bootstrap_pct = 0       # Last observed Tor bootstrap percentage
        self._bootstrap_stall_count = 0    # Consecutive checks with no bootstrap progress
        self._yellow_since = None          # Timestamp when entered yellow state
        self._was_ready = False            # Were we ever ready this session?
        self._tor_internally_ready = False # Checks 1-4 passed (Arti+WordPress up)
        self._onionheaven_reclaim_succeeded = False  # Whether /online reclaim succeeded
        self._onionheaven_reclaim_in_flight = False  # Whether a reclaim thread is running
        self._onionheaven_reclaim_last_attempt = 0   # Timestamp of last reclaim attempt
        self._tor_last_auto_restart = 0    # Timestamp of last auto-restart (cooldown-based)
        self._wordpress_confirmed = False  # WordPress responded at least once (stays up reliably)
        self.healthcheck_address = None    # Healthcheck .onion address
        self.onionheaven_messages = []          # Messages received from OnionHeaven
        self._onionheaven_alert_shown = False   # Whether we've shown OnionHeaven alert icon
        self.is_onionheaven = False             # True if this instance is OnionHeaven
        self._onionheaven_checked = False       # Whether onionheaven mode has been checked
        self._onionheaven_registration_succeeded = False  # Whether registration succeeded
        self._onionheaven_registration_in_flight = False  # Whether registration thread is running
        self.cloudflare_tunnel_enabled = False  # True when CLOUDFLARE_TUNNEL_TOKEN is set
        self._quitting = False                 # True once quit cleanup has started
        self._stopping = False                 # True while Stop button is in progress
        self._run_generation = 0               # Incremented on stop/start; stale threads check this

        # Wayback queue state
        self._wayback_queue = []
        self._wayback_queue_lock = threading.Lock()
        self._wayback_last_drain = 0
        self._wayback_queue_item = rumps.MenuItem("", callback=None)

        # Menu items
        # Store reference to browser menu item so we can update its title
        self.browser_menu_item = rumps.MenuItem("Open in Tor Browser", callback=self.open_tor_browser)
        self.local_site_item = rumps.MenuItem("Open Local Site", callback=self.open_local_site)
        self.onionheaven_alert_item = rumps.MenuItem("OnionHeaven Alerts", callback=self.view_onionheaven_alerts)
        self.clearnet_status_item = rumps.MenuItem("", callback=None)

        self.menu = [
            rumps.MenuItem("Starting...", callback=None),
            rumps.separator,
            rumps.MenuItem("Copy Onion Address", callback=self.copy_address),
            self.browser_menu_item,
            self.local_site_item,
            rumps.separator,
            rumps.MenuItem("Start", callback=self.start_service),
            rumps.MenuItem("Stop", callback=self.stop_service),
            rumps.MenuItem("Restart", callback=self.restart_service),
            rumps.separator,
            rumps.MenuItem("View Logs", callback=self.view_logs),
            rumps.MenuItem("View Web Usage Log", callback=self.view_web_log),
            rumps.MenuItem("Settings...", callback=self.open_settings),
            rumps.separator,
            rumps.MenuItem("Backup...", callback=self.backup),
            rumps.MenuItem("Restore...", callback=self.restore),
            rumps.separator,
            rumps.MenuItem("Check for Updates...", callback=self.check_for_updates),
            rumps.MenuItem("About OnionPress", callback=self.show_about),
            rumps.MenuItem("Uninstall...", callback=self.uninstall),
            rumps.separator,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # Ensure Docker is available
        threading.Thread(target=self.ensure_docker_available, daemon=True).start()

        # Listen for system wake to immediately mark Tor as reconnecting
        self.register_wake_notification()

        # Start status checker
        self.start_status_checker()

        # Auto-start on launch
        threading.Thread(target=self.auto_start, daemon=True).start()

    def show_launch_splash(self):
        """Show non-blocking launch splash with logo - no I/O blocking"""
        def show():
            try:
                # Create window (no I/O) - taller for buttons and time estimate
                window = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                    AppKit.NSMakeRect(0, 0, 320, 300),
                    AppKit.NSWindowStyleMaskTitled,  # No close button - dismisses automatically when ready
                    AppKit.NSBackingStoreBuffered,
                    False
                )
                window.setTitle_("OnionPress")
                window.setLevel_(AppKit.NSFloatingWindowLevel)
                window.center()
                window.setReleasedWhenClosed_(False)  # Keep window object alive
                window.setHidesOnDeactivate_(False)  # Stay visible when clicking other windows

                # Create content view
                content_view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 320, 300))

                # Add "Launching..." text (no I/O)
                text_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(60, 120, 200, 30))
                text_field.setStringValue_("Launching OnionPress...")
                text_field.setBezeled_(False)
                text_field.setDrawsBackground_(False)
                text_field.setEditable_(False)
                text_field.setSelectable_(False)
                text_field.setAlignment_(AppKit.NSTextAlignmentCenter)
                font = AppKit.NSFont.systemFontOfSize_(16)
                text_field.setFont_(font)
                content_view.addSubview_(text_field)

                # Add estimated time text
                time_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(40, 90, 240, 20))
                time_field.setStringValue_("Estimated time: ~3 minutes")
                time_field.setBezeled_(False)
                time_field.setDrawsBackground_(False)
                time_field.setEditable_(False)
                time_field.setSelectable_(False)
                time_field.setAlignment_(AppKit.NSTextAlignmentCenter)
                time_field.setTextColor_(AppKit.NSColor.secondaryLabelColor())
                small_font = AppKit.NSFont.systemFontOfSize_(12)
                time_field.setFont_(small_font)
                content_view.addSubview_(time_field)

                # Add View Log button
                view_log_button = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(20, 20, 130, 32))
                view_log_button.setTitle_("View Log")
                view_log_button.setBezelStyle_(AppKit.NSBezelStyleRounded)
                view_log_button.setTarget_(self)
                view_log_button.setAction_("openLogFile:")
                content_view.addSubview_(view_log_button)

                # Add Dismiss button
                dismiss_button = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(170, 20, 130, 32))
                dismiss_button.setTitle_("Dismiss")
                dismiss_button.setBezelStyle_(AppKit.NSBezelStyleRounded)
                dismiss_button.setTarget_(self)
                dismiss_button.setAction_("dismissSplashButton:")
                content_view.addSubview_(dismiss_button)

                window.setContentView_(content_view)
                window.makeKeyAndOrderFront_(None)

                self.launch_splash = window
                self.launch_splash_time_field = time_field  # Store reference for updates

                # Log splash creation
                try:
                    with open(self.log_file, 'a') as f:
                        f.write(f"DEBUG: Launch splash created and shown\n")
                except Exception:
                    pass

                # Add logo on main thread (fast local PNG load, avoids AppKit threading crash)
                icon_path = os.path.join(self.resources_dir, "app-icon.png")
                if os.path.exists(icon_path):
                    image_view = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(110, 180, 100, 100))
                    image = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                    if image:
                        image_view.setImage_(image)
                        content_view.addSubview_(image_view)

            except Exception as e:
                pass  # Don't log yet, log file not ready

        # Show on main thread
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(show)

    def dismiss_launch_splash(self):
        """Dismiss the launch splash window"""
        def dismiss():
            if self.launch_splash:
                try:
                    self.log("Dismissing launch splash")
                    self.launch_splash.orderOut_(None)
                    self.launch_splash.close()
                    self.launch_splash = None
                except Exception as e:
                    self.log(f"Error dismissing launch splash: {e}")

        # Dismiss on main thread
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(dismiss)

    def openLogFile_(self, sender):
        """Action handler for View Log button — open in built-in log viewer"""
        try:
            _LogViewerWindow.show_for_file(self.log_file, "OnionPress Log")
        except Exception as e:
            self.log(f"Error opening log file: {e}")

    def dismissSplashButton_(self, sender):
        """Action handler for Dismiss button"""
        self.dismiss_launch_splash()

    def log(self, message):
        """Write log message to onionpress.log file"""
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_message = f"[{timestamp}] {message}\n"
            fd = os.open(self.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as f:
                f.write(log_message)
        except Exception as e:
            print(f"Error writing to log: {e}")

    def _caffeinate_pid_file(self):
        """Path to the file tracking our caffeinate PID."""
        return os.path.join(self.app_support, "caffeinate.pid")

    def _cleanup_stale_caffeinate(self):
        """Kill any orphaned caffeinate process from a previous OnionPress run."""
        pid_file = self._caffeinate_pid_file()
        if not os.path.exists(pid_file):
            return
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            # Verify it's actually a caffeinate process before killing
            result = subprocess.run(
                ["ps", "-p", str(old_pid), "-o", "comm="],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5
            )
            if result.returncode == 0 and "caffeinate" in result.stdout:
                os.kill(old_pid, 15)  # SIGTERM
                self.log(f"Cleaned up orphaned caffeinate (PID {old_pid}) from previous run")
            os.remove(pid_file)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            try:
                os.remove(pid_file)
            except OSError:
                pass

    def start_caffeinate(self):
        """Start caffeinate to prevent Mac from sleeping based on config mode"""
        # Check if already running
        if self.caffeinate_process is not None:
            try:
                # Check if process is still alive
                if self.caffeinate_process.poll() is None:
                    return  # Already running
            except Exception:
                pass

        # Clean up any orphaned caffeinate from a previous crash/force-quit
        self._cleanup_stale_caffeinate()

        # Read config — 3 modes: normal, on-battery, never
        mode = self.read_config_value("PREVENT_SLEEP", "normal").lower()
        # Backward compat: yes→on-battery, no→normal
        if mode == "yes":
            mode = "on-battery"
        elif mode == "no":
            mode = "normal"

        if mode == "on-battery":
            caff_args = ["caffeinate", "-s"]
            caff_msg = "staying awake on AC power"
        elif mode == "never":
            caff_args = ["caffeinate", "-i"]
            caff_msg = "never sleeping while OnionPress runs"
        else:
            # "normal" or unknown — no caffeinate
            return

        try:
            self.caffeinate_process = subprocess.Popen(
                caff_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            # Write PID file so we can clean up if we crash
            try:
                with open(self._caffeinate_pid_file(), 'w') as f:
                    f.write(str(self.caffeinate_process.pid))
            except OSError:
                pass
            self.log(f"Started caffeinate (PID {self.caffeinate_process.pid}) - {caff_msg}")
        except Exception as e:
            self.log(f"Failed to start caffeinate: {e}")

    def stop_caffeinate(self):
        """Stop caffeinate to allow Mac to sleep normally"""
        if self.caffeinate_process is not None:
            try:
                self.caffeinate_process.terminate()
                self.caffeinate_process.wait(timeout=2)
                self.log("Stopped caffeinate - Mac can sleep normally")
            except Exception as e:
                # Force kill if terminate doesn't work
                try:
                    self.caffeinate_process.kill()
                    self.log("Force killed caffeinate process")
                except Exception:
                    pass
            finally:
                self.caffeinate_process = None
                # Remove PID file
                try:
                    os.remove(self._caffeinate_pid_file())
                except OSError:
                    pass

    def start_onion_proxy(self):
        """Start the local .onion proxy server in a background thread."""
        if self.proxy_server is not None:
            return  # already running

        docker_bin = os.path.join(self.bin_dir, "docker")
        docker_env = os.environ.copy()
        docker_env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        docker_env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

        # Install the PHP proxy script into the WordPress container
        php_script = os.path.join(self.script_dir, "onion-forward.php")
        if not os.path.exists(php_script):
            # Fallback: check parent resources dir
            php_script = os.path.join(self.parent_resources_dir, "onion-forward.php")
        onion_proxy.install_php_proxy(docker_bin, docker_env, php_script, log_func=self.log)

        def run_proxy():
            try:
                server = onion_proxy.ThreadingHTTPServer(
                    ("127.0.0.1", self.proxy_port),
                    onion_proxy.OnionProxyHandler
                )
                server.docker_bin = docker_bin
                server.docker_env = docker_env
                server.onion_address = self.onion_address
                server.healthcheck_address = self.healthcheck_address
                server.version = self.version
                server.data_dir = self.app_support
                server.log_func = self.log
                server.launcher_script = self.launcher_script
                self.proxy_server = server
                self.log(f"Onion proxy listening on http://127.0.0.1:{self.proxy_port}")
                server.serve_forever()
            except Exception as e:
                self.log(f"Onion proxy failed to start: {e}")
                self.proxy_server = None

        self.proxy_thread = threading.Thread(target=run_proxy, daemon=True)
        self.proxy_thread.start()

    def stop_onion_proxy(self):
        """Stop the local .onion proxy server."""
        if self.proxy_server is not None:
            try:
                self.proxy_server.shutdown()
                self.log("Onion proxy stopped")
            except Exception as e:
                self.log(f"Error stopping onion proxy: {e}")
            finally:
                self.proxy_server = None
                self.proxy_thread = None

    def check_wp_installed(self):
        """Check if WordPress core is installed via wp-cli.

        Returns True (installed), False (not installed), or None (container not ready).
        """
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-wordpress",
                 "wp", "core", "is-installed", "--allow-root"],
                env=env, capture_output=True, timeout=10
            )
            return result.returncode == 0
        except Exception:
            return None

    def show_native_alert(self, title, message, buttons=["OK"], default_button=0, cancel_button=None, style="informational"):
        """Show a native macOS alert dialog using AppKit (no permission prompts, shows custom icon)

        Args:
            title: Dialog title
            message: Dialog message text
            buttons: List of button labels (default: ["OK"])
            default_button: Index of default button (default: 0)
            cancel_button: Index of cancel button or None (default: None)
            style: "informational", "warning", or "critical" (default: "informational")

        Returns:
            Index of clicked button (0-based), or None if dialog dismissed
        """
        def show_dialog():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)

            # Set alert style
            if style == "warning":
                alert.setAlertStyle_(AppKit.NSAlertStyleWarning)
            elif style == "critical":
                alert.setAlertStyle_(AppKit.NSAlertStyleCritical)
            else:
                alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

            # Add buttons (first button is default)
            for i, button_text in enumerate(buttons):
                btn = alert.addButtonWithTitle_(button_text)
                if i == default_button:
                    btn.setKeyEquivalent_("\r")  # Return key
                elif cancel_button is not None and i == cancel_button:
                    btn.setKeyEquivalent_("\x1b")  # Escape key

            # Set app icon if available
            icon_path = os.path.join(self.resources_dir, "app-icon.png")
            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    alert.setIcon_(icon)

            # Show modal dialog and get response
            response = alert.runModal()

            # Convert response to button index
            # NSAlertFirstButtonReturn = 1000, second = 1001, etc.
            button_index = response - 1000
            return button_index if button_index >= 0 else None

        # Must run on main thread
        # Check if we're already on the main thread to avoid deadlock
        if AppKit.NSThread.isMainThread():
            # Already on main thread, run directly
            return show_dialog()
        else:
            # Not on main thread, dispatch to main thread and wait
            result_container = [None]
            def run_on_main():
                result_container[0] = show_dialog()

            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(run_on_main)

            # Wait for result (with timeout)
            max_wait = 300  # 5 minutes
            waited = 0
            while result_container[0] is None and waited < max_wait:
                time.sleep(0.1)
                waited += 0.1

            return result_container[0]

    def log_version_info(self):
        """Log version information for all components at startup"""
        self.log("=" * 60)
        self.log(f"OnionPress v{self.version} starting up")
        self.startup_time = time.time()
        self.log("=" * 60)

        # macOS version
        try:
            result = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
            macos_version = result.stdout.strip() if result.returncode == 0 else "Unknown"
            self.log(f"macOS version: {macos_version}")
        except Exception:
            pass

        # Colima version
        try:
            colima_bin = os.path.join(self.bin_dir, "colima")
            if os.path.exists(colima_bin):
                result = subprocess.run([colima_bin, "version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                colima_version = result.stdout.strip().split('\n')[0] if result.returncode == 0 else "Unknown"
                self.log(f"Colima version: {colima_version}")
        except Exception:
            pass

        # Docker version
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            if os.path.exists(docker_bin):
                result = subprocess.run([docker_bin, "--version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                docker_version = result.stdout.strip() if result.returncode == 0 else "Unknown"
                self.log(f"Docker version: {docker_version}")
        except Exception:
            pass

        # Docker Compose version
        try:
            compose_bin = os.path.join(self.bin_dir, "docker-compose")
            if os.path.exists(compose_bin):
                result = subprocess.run([compose_bin, "version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                compose_version = result.stdout.strip().split('\n')[0] if result.returncode == 0 else "Unknown"
                self.log(f"Docker Compose version: {compose_version}")
        except Exception:
            pass

        # Log cached onion address from previous run if available
        try:
            cached_addr_file = os.path.join(self.app_support, "onion_address")
            with open(cached_addr_file) as f:
                cached = f.read().strip()
            if cached and cached.endswith('.onion'):
                self.log(f"Onion address: {cached}")
        except (OSError, IOError):
            pass

        self.log("=" * 60)

    def _web_log_reader_thread(self, process, raw_path, filtered_path):
        """Read docker logs and write to both raw and filtered log files"""
        try:
            with open(raw_path, 'a') as raw_f, open(filtered_path, 'a') as filtered_f:
                for line in process.stdout:
                    raw_f.write(line)
                    raw_f.flush()
                    if "OnionPress-HealthCheck" not in line:
                        filtered_f.write(line)
                        filtered_f.flush()
        except Exception:
            pass

    def start_web_log_capture(self):
        """Start capturing WordPress logs to a file"""
        if self.web_log_process is not None:
            return  # Already running

        try:
            web_log_file = os.path.join(self.app_support, "wordpress-access.log")
            visitors_log_file = os.path.join(self.app_support, "wordpress-visitors.log")
            docker_bin = os.path.join(self.bin_dir, "docker")

            # Start docker logs process in background, capture stdout as text
            self.web_log_process = subprocess.Popen(
                [docker_bin, "logs", "-f", "--tail", "100", "onionpress-wordpress"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                env={
                    "DOCKER_HOST": f"unix://{self.colima_home}/default/docker.sock"
                }
            )

            # Start reader thread that splits logs into raw + filtered files
            self.web_log_thread = threading.Thread(
                target=self._web_log_reader_thread,
                args=(self.web_log_process, web_log_file, visitors_log_file),
                daemon=True
            )
            self.web_log_thread.start()

            print(f"Started web log capture to {web_log_file}")
        except Exception as e:
            print(f"Error starting web log capture: {e}")
            self.web_log_process = None

    def stop_web_log_capture(self):
        """Stop capturing WordPress logs"""
        if self.web_log_process is not None:
            try:
                self.web_log_process.terminate()
                self.web_log_process.wait(timeout=5)
            except Exception:
                try:
                    self.web_log_process.kill()
                except Exception:
                    pass
            self.web_log_process = None
            # Wait for reader thread to finish
            if hasattr(self, 'web_log_thread') and self.web_log_thread:
                self.web_log_thread.join(timeout=3)
                self.web_log_thread = None
            print("Stopped web log capture")

    def ensure_docker_available(self):
        """Ensure bundled Colima is running (no-op during first-time setup as launcher handles it)"""
        try:
            # During first-time setup, the launcher script handles Colima initialization
            # So we just check if it's ready, but don't try to start it ourselves
            colima_bin = os.path.join(self.bin_dir, "colima")
            if not os.path.exists(colima_bin):
                self.log("ERROR: Bundled Colima not found")
                return

            # Check if running
            result = subprocess.run([colima_bin, "status"], capture_output=True, timeout=5)

            if result.returncode == 0:
                # Verify docker accessible
                docker_check = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
                if docker_check.returncode == 0:
                    self.log("Bundled Colima is running")
                    return

            # Don't try to start Colima here - the launcher script handles initialization
            # This avoids conflicts during first-time setup
            self.log("Colima not running yet (launcher may still be initializing)")

        except Exception as e:
            self.log(f"Error checking Colima: {e}")

    def check_port_conflict(self):
        """Check if required ports are already in use by another process."""
        ports = [self.wp_port, self.socks_port, self.proxy_port]
        in_use = []
        for port in ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.bind(('127.0.0.1', port))
                s.close()
            except OSError:
                in_use.append(port)
        return in_use

    def auto_start(self):
        """Automatically start the service when the app launches"""
        time.sleep(1)  # Brief delay

        # Wait for Colima to be ready (important for first-time setup)
        self.log("Waiting for container runtime to be ready...")
        docker_bin = os.path.join(self.bin_dir, "docker")
        colima_initialized = os.path.join(self.colima_home, ".initialized")

        # Wait up to 3 minutes for Colima initialization
        max_wait = 180  # 3 minutes
        waited = 0
        while waited < max_wait:
            # Check if Colima is initialized and docker is responding
            if os.path.exists(colima_initialized):
                try:
                    result = subprocess.run(
                        [docker_bin, "info"],
                        capture_output=True,
                        timeout=5,
                        env=os.environ.copy()
                    )
                    if result.returncode == 0:
                        self.log("Container runtime is ready")
                        break
                except Exception:
                    pass

            time.sleep(3)
            waited += 3

        if waited >= max_wait:
            self.log("WARNING: Container runtime not ready after 3 minutes")

        # Check for port conflicts (another user's OnionPress or other process)
        # Only flag a conflict if ports are busy AND our own containers aren't running.
        # Retry a few times since a previous instance may still be releasing ports.
        in_use = self.check_port_conflict()
        if in_use:
            for retry in range(5):
                self.log(f"Ports {in_use} busy, waiting for previous instance to release ({retry+1}/5)...")
                time.sleep(2)
                in_use = self.check_port_conflict()
                if not in_use:
                    break
        if in_use:
            # Check if our containers are already running (normal restart case)
            try:
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                result = subprocess.run(
                    [docker_bin, "ps", "--format", "{{.Names}}"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5, env=env
                )
                our_containers = result.stdout.strip()
            except Exception:
                our_containers = ""

            if "onionpress-" not in our_containers:
                ports_str = ', '.join(str(p) for p in in_use)
                self.log(f"Port conflict detected: ports {ports_str} already in use by another process")
                self._port_conflict = True
                # Must dispatch to main thread — rumps.alert() requires it
                AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
                    lambda: rumps.alert(
                        title="OnionPress Cannot Start",
                        message=f"Port(s) {ports_str} already in use.\n\n"
                                "Another process is using these ports.\n\n"
                                "Close the conflicting application and try again."
                    )
                )
                self.menu["Starting..."].title = "Status: Port conflict"
                return

        self._ports_checked = True

        # Check if UPDATE_ON_LAUNCH is enabled
        config_file = os.path.join(self.app_support, "config")
        update_on_launch = False
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        if line.startswith('UPDATE_ON_LAUNCH='):
                            value = line.split('=', 1)[1].strip().lower()
                            update_on_launch = (value == 'yes')
                            break
            except Exception:
                pass

        if update_on_launch:
            self.log("UPDATE_ON_LAUNCH enabled - checking for Docker image updates...")
            self.update_docker_images(show_notifications=False)

        self.start_service(None)


    LAUNCHAGENT_LABEL = "com.onionpress.launcher"
    LAUNCHAGENT_PATH = os.path.expanduser(
        f"~/Library/LaunchAgents/{LAUNCHAGENT_LABEL}.plist")

    def _is_login_item_installed(self):
        """Check if LaunchAgent plist exists"""
        return os.path.exists(self.LAUNCHAGENT_PATH)

    def add_login_item(self):
        """Install LaunchAgent plist for auto-start on login"""
        try:
            plist = {
                "Label": self.LAUNCHAGENT_LABEL,
                "ProgramArguments": ["open", "-a", "/Applications/OnionPress.app"],
                "RunAtLoad": True,
                "LimitLoadToSessionType": "Aqua",
            }
            os.makedirs(os.path.dirname(self.LAUNCHAGENT_PATH), exist_ok=True)
            with open(self.LAUNCHAGENT_PATH, "wb") as f:
                plistlib.dump(plist, f)
            self.log("LaunchAgent installed for login auto-start")
            return True
        except Exception as e:
            self.log(f"Error installing LaunchAgent: {e}")
            return False

    def remove_login_item(self):
        """Remove LaunchAgent plist"""
        try:
            if os.path.exists(self.LAUNCHAGENT_PATH):
                # Unload first (ignore errors if not loaded)
                subprocess.run(
                    ["launchctl", "bootout", f"gui/{os.getuid()}",
                     self.LAUNCHAGENT_PATH],
                    capture_output=True, timeout=10)
                os.remove(self.LAUNCHAGENT_PATH)
            self.log("LaunchAgent removed")
            return True
        except Exception as e:
            self.log(f"Error removing LaunchAgent: {e}")
            return False


    def run_command(self, command):
        """Run a command and return output"""
        try:
            result = subprocess.run(
                [self.launcher_script, command],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=60
            )
            return result.stdout.strip()
        except Exception as e:
            print(f"Error running command {command}: {e}")
            return None

    def check_wordpress_health(self, log_result=True):
        """Check if WordPress is actually responding to requests"""
        try:
            if log_result:
                self.log(f"Checking local access: http://localhost:{self.wp_port}")
            # Use curl instead of urllib to avoid "local network" permission prompt
            result = subprocess.run(
                ["curl", "-s", "--max-time", "3", "-H", "User-Agent: OnionPress-HealthCheck", f"http://localhost:{self.wp_port}"],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=5
            )
            if result.returncode == 0:
                content = result.stdout
                # Check for database errors or WordPress not ready
                if 'Error establishing a database connection' in content:
                    if log_result:
                        self.log("✗ Local access: Database connection error")
                    return False
                if 'Database connection error' in content:
                    if log_result:
                        self.log("✗ Local access: Database connection error")
                    return False
                # If we get here and got a response, WordPress is responding
                # Either it's the install page or actual WordPress content
                if log_result:
                    self.log("✓ Local access: WordPress responding")
                return True
            else:
                if log_result:
                    self.log(f"✗ Local access: Connection failed (curl exit code {result.returncode})")
                return False
        except Exception as e:
            if log_result:
                self.log(f"✗ Local access: Connection failed ({str(e)})")
            return False

    def check_tor_reachability(self, log_result=True):
        """Check if the .onion service is properly configured and published"""
        self._tor_internally_ready = False
        if not self.onion_address or self.onion_address in ["Starting...", "Not running", "Generating address..."]:
            return False

        try:
            if log_result:
                self.log(f"Checking Tor onion service status for: {self.onion_address}")

            docker_bin = os.path.join(self.bin_dir, "docker")

            # Set up environment for docker commands
            docker_env = os.environ.copy()
            docker_env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            docker_env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

            # Check 1: Verify hostname file exists and matches
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor",
                 "cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=5,
                env=docker_env
            )

            if result.returncode != 0:
                if log_result:
                    self.log(f"✗ Onion service hostname file not found")
                return False

            hostname = result.stdout.strip()
            if hostname != self.onion_address:
                if log_result:
                    self.log(f"✗ Hostname mismatch: {hostname} != {self.onion_address}")
                return False

            # Check 2: Verify Tor has bootstrapped
            # Use full logs — the bootstrap message is logged once per startup
            # and can be pushed out of --tail by HSDir query spam.
            # Arti: "Sufficiently bootstrapped", C Tor: "Bootstrapped 100% (done)"
            bootstrap_result = subprocess.run(
                [docker_bin, "logs", "onionpress-tor"],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=10,
                env=docker_env
            )

            tor_output = bootstrap_result.stdout + bootstrap_result.stderr
            if "Sufficiently bootstrapped" not in tor_output and "Bootstrapped 100% (done)" not in tor_output:
                if log_result:
                    self.log(f"✗ Tor not fully bootstrapped yet")
                return False

            # Check 3: Verify no critical errors in recent logs
            # (Arti uses "ERROR" log level normally, so check for specific failure messages)
            if "failed to publish" in tor_output.lower():
                if log_result:
                    self.log(f"✗ Tor errors detected in logs")
                return False

            # Check 4: Verify WordPress is reachable from Tor container
            # (SOCKS proxy at 127.0.0.1:9050 doesn't work through Colima VM
            # port forwarding, so we test the actual path: tor -> wordpress
            # over the Docker network using docker exec + wget)
            probe_result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor",
                 "wget", "-q", "-O", "/dev/null", "--timeout=5",
                 "-U", "OnionPress-HealthCheck",
                 "http://wordpress:80/"],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=10,
                env=docker_env
            )
            if probe_result.returncode != 0:
                if log_result:
                    self.log(f"✗ WordPress not reachable from Tor container")
                return False

            # Checks 1-4 passed — mark internally ready
            self._tor_internally_ready = True

            # Check 5: Verify onion service is actually reachable through Tor network
            # Uses the independent tor-client container (not onionpress-tor which hosts
            # the service and can resolve its own .onion via self-connection shortcut)
            probe_result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor-client",
                 "curl", "-s", "--socks5-hostname", "127.0.0.1:9050",
                 "--max-time", "10", "-o", "/dev/null", "-w", "%{http_code}",
                 "-H", "User-Agent: OnionPress-HealthCheck",
                 f"http://{self.onion_address}/"],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=15,
                env=docker_env
            )
            if probe_result.returncode != 0 or probe_result.stdout.strip() not in ["200", "301"]:
                if log_result:
                    self.log(f"✗ Onion service not yet reachable through Tor network")
                return False

            if log_result:
                self.log(f"✓ Onion service verified: {self.onion_address}")

            return True

        except Exception as e:
            if log_result:
                self.log(f"✗ Tor status check failed: {str(e)}")
            return False

    def _remove_pid_file(self):
        """Remove PID file on exit"""
        try:
            if os.path.exists(self.pid_file):
                with open(self.pid_file) as f:
                    pid = int(f.read().strip())
                if pid == os.getpid():
                    os.remove(self.pid_file)
        except Exception:
            pass

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM/SIGINT — trigger graceful quit (same as Quit button)"""
        self.log(f"Received signal {signum}, initiating graceful shutdown...")
        _main_thread(lambda: self.quit_app(None))

    def handle_reopen(self):
        """Handle reopen signal from launcher (user double-clicked app while running)"""
        self.log("Reopen signal received")
        if self.is_running and self.is_ready:
            self.log("Service is ready — opening browser")
            self.open_tor_browser(None)
        elif not self.is_running:
            self.log("Service not running — starting service")
            self.start_service(None)

    def check_internet_connectivity(self):
        """Check if host has internet connectivity.
        Uses curl subprocess to avoid macOS 'local network' permission prompt
        that Python's socket module triggers."""
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "3", "-o", "/dev/null", "-w", "%{http_code}",
                 "http://1.1.1.1/"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def _parse_bootstrap_percentage(self):
        """Parse Tor bootstrap percentage from full container logs.
        Returns highest percentage found (0-100), or 0 if not parseable.
        Uses full logs since bootstrap messages can be pushed out of --tail
        by HSDir query spam when many onion descriptors are being fetched."""
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            docker_env = os.environ.copy()
            docker_env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            docker_env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            result = subprocess.run(
                [docker_bin, "logs", "onionpress-tor"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10,
                env=docker_env
            )
            output = result.stdout + result.stderr
            best = 0
            # Arti: "Sufficiently bootstrapped; proxy now functional" = 100%
            if "Sufficiently bootstrapped" in output:
                return 100
            for line in output.splitlines():
                idx = line.find("Bootstrapped ")
                if idx >= 0:
                    rest = line[idx + len("Bootstrapped "):]
                    pct_str = ""
                    for ch in rest:
                        if ch.isdigit():
                            pct_str += ch
                        else:
                            break
                    if pct_str:
                        val = int(pct_str)
                        if val > best:
                            best = val
            return best
        except Exception:
            return 0

    @property
    def display_state(self):
        """Compute the display state from current variables.
        Returns one of: 'stopped', 'available', 'offline', 'stuck', 'starting'."""
        if not self.is_running:
            return "stopped"
        if self.is_ready:
            return "available"
        if not self._has_internet:
            return "offline"
        # Check for stuck: yellow 5min+ (gives auto-restart time to work)
        if self._yellow_since and (time.time() - self._yellow_since) > 300:
            return "stuck"
        return "starting"

    def _read_config_value(self, key, default=""):
        """Read a value from ~/.onionpress/config."""
        config_file = os.path.join(self.app_support, "config")
        try:
            with open(config_file, encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{key}="):
                        return line.split("=", 1)[1]
        except (OSError, IOError):
            pass
        return default

    def check_status(self):
        """Check if containers are running and get onion address"""
        if self._port_conflict:
            return
        with self._checking_lock:
            if self.checking:
                return
            self.checking = True

        try:
            # Check for reopen signal from launcher
            reopen_file = os.path.join(self.app_support, ".reopen")
            if os.path.exists(reopen_file):
                try:
                    os.remove(reopen_file)
                except OSError:
                    pass
                self.handle_reopen()

            # Check if containers are running
            status_json = self.run_command("status")

            if status_json and status_json != "[]":
                try:
                    status = json.loads(status_json)
                    self.is_running = len(status) > 0 and all(
                        s.get("State", "").lower() == "running" for s in status
                    )
                except Exception:
                    self.is_running = False
            else:
                self.is_running = False

            # Get onion address if running
            if self.is_running:
                addr = self.run_command("address")
                if addr and addr != "Generating...":
                    self.onion_address = addr.strip()
                    # Cache address locally for instant availability on next launch
                    try:
                        with open(os.path.join(self.app_support, "onion_address"), 'w') as f:
                            f.write(self.onion_address)
                    except OSError:
                        pass
                else:
                    self.onion_address = "Generating address..."

                # Check internet connectivity
                had_internet = self._has_internet
                self._has_internet = self.check_internet_connectivity()
                if not self._has_internet and had_internet:
                    self.log("Internet connectivity lost")
                elif self._has_internet and not had_internet:
                    self.log("Internet connectivity restored")

                if not self._has_internet:
                    # No internet — skip expensive WordPress/Tor checks
                    if self.is_ready:
                        self.log("Going offline — no internet connection")
                    self.is_ready = False
                    # Track yellow/starting state
                    if self._yellow_since is None:
                        self._yellow_since = time.time()
                else:
                    # Internet available — do full health checks
                    # Determine if we should do detailed checks and logging
                    current_status = (self.is_running, self.onion_address)
                    should_log = (current_status != self.last_status_logged) or not self.is_ready

                    # Check if WordPress is ready and Tor is reachable.
                    # Once WordPress responds, skip rechecking it — it stays up
                    # reliably inside Docker. Only Tor needs ongoing monitoring.
                    if not self._wordpress_confirmed:
                        wordpress_ready = self.check_wordpress_health(log_result=should_log)
                        if wordpress_ready:
                            self._wordpress_confirmed = True
                    else:
                        wordpress_ready = True
                    tor_reachable = self.check_tor_reachability(log_result=should_log)

                    previous_ready = self.is_ready
                    ready_now = wordpress_ready and tor_reachable

                    if ready_now and not previous_ready:
                        self.is_ready = True
                        self._was_ready = True
                        self._onionheaven_reclaim_succeeded = False
                        self._onionheaven_reclaim_in_flight = False
                        self._onionheaven_reclaim_last_attempt = 0
                        self._bootstrap_stall_count = 0
                        self._yellow_since = None
                        elapsed = int(time.time() - self.startup_time)
                        self.log(f"✓ System fully operational (launched in {elapsed}s)")
                        self.last_status_logged = current_status

                        # Re-read Cloudflare Tunnel config (may have changed since launch)
                        self.cloudflare_tunnel_enabled = bool(self._read_config_value("CLOUDFLARE_TUNNEL_TOKEN"))

                        # Dismiss setup dialog if it's showing
                        self.dismiss_setup_dialog()

                        # Auto-open browser on first ready (runs in background
                        # so the monitoring loop can continue and start the proxy)
                        if not self.auto_opened_browser:
                            self.auto_opened_browser = True
                            self.log(f"DEBUG: Spawning auto_open_browser thread, onion_address={self.onion_address!r}")
                            threading.Thread(target=self.auto_open_browser, daemon=True).start()

                        # Force menu update (changes icon to purple)
                        self.update_menu()

                        # Dismiss splash AFTER icon turns purple
                        self.dismiss_launch_splash()
                    elif ready_now:
                        # Already was ready, keep it ready
                        self.is_ready = True
                        self._bootstrap_stall_count = 0
                        self._yellow_since = None
                        self.last_status_logged = current_status
                    elif previous_ready and not ready_now:
                        # Was ready, now failing — go to reconnecting state
                        # (but skip if user intentionally stopped or is quitting)
                        if self._stopping or self._quitting:
                            self.is_ready = False
                        else:
                            self.is_ready = False
                            self._yellow_since = time.time()
                            self._bootstrap_stall_count = 0
                            self._tor_last_auto_restart = 0  # Allow immediate auto-restart
                            self.log("Service became unreachable — reconnecting")
                    else:
                        # Not ready yet — track bootstrap progress for stuck detection
                        pct = self._parse_bootstrap_percentage()
                        if pct > self._last_bootstrap_pct:
                            self._last_bootstrap_pct = pct
                            self._bootstrap_stall_count = 0
                        else:
                            self._bootstrap_stall_count += 1
                        if self._yellow_since is None:
                            self._yellow_since = time.time()

                        # Auto-restart tor if stuck for 2+ minutes AND
                        # the container shows signs of actual trouble (broken
                        # guards, circuit failures). If Arti is healthy but
                        # just waiting for descriptor propagation, don't restart
                        # — that would reset progress.
                        # Uses cooldown (5 min) so we can retry if the spiral recurs.
                        if (self._yellow_since
                                and not self._stopping
                                and not self._quitting
                                and (time.time() - self._yellow_since) > 120
                                and (time.time() - self._tor_last_auto_restart) > 300
                                and self._tor_container_unhealthy()):
                            self._tor_last_auto_restart = time.time()
                            self.log("Tor container unhealthy after 2min — restarting")
                            threading.Thread(target=self._auto_restart_tor, daemon=True).start()

                # Start web log capture if not already running
                if self.web_log_process is None:
                    threading.Thread(target=self.start_web_log_capture, daemon=True).start()

                # Start caffeinate if not already running (prevents sleep while service runs)
                if self.caffeinate_process is None or self.caffeinate_process.poll() is not None:
                    self.start_caffeinate()

                # Start onion proxy if not already running (wait for port check first)
                if self.proxy_server is None and self._ports_checked:
                    self.start_onion_proxy()
                elif self.proxy_server:
                    # Update onion address and readiness on existing proxy
                    self.proxy_server.onion_address = self.onion_address
                    self.proxy_server.healthcheck_address = self.healthcheck_address
                    self.proxy_server.tor_ready = self.is_ready

                # Read healthcheck address if not yet known
                if self.healthcheck_address is None and self.is_ready:
                    self.read_healthcheck_address()

                # Poll for OnionHeaven messages from healthcheck service
                if self.is_ready:
                    self.poll_onionheaven_messages()

                # Poll and drain Wayback queue
                self.poll_wayback_queue()
                if self.is_ready:
                    self.drain_wayback_queue()

                # Write status, poll for config updates & action requests from WordPress settings page
                self.write_status_to_volume()
                self.poll_config_updates()
                self.poll_requested_actions()

                # OnionHeaven: detect onionheaven mode (one-shot)
                if self.is_ready and not self._onionheaven_checked:
                    self._onionheaven_checked = True
                    if onionheaven.is_onionheaven_instance(self.onion_address):
                        self.is_onionheaven = True
                        self.log("OnionHeaven mode activated (heartbeat monitor runs in onionheaven container)")
                        # One-shot: auto-set PREVENT_SLEEP=never on first OnionHeaven detection
                        # Uses a marker file so we never override the user's later choice
                        sleep_marker = os.path.join(self.app_support, ".onionheaven_sleep_set")
                        if not os.path.exists(sleep_marker):
                            self.write_config_value("PREVENT_SLEEP", "never")
                            try:
                                with open(sleep_marker, 'w') as f:
                                    f.write("1")
                            except OSError:
                                pass
                            self.log("Auto-set PREVENT_SLEEP=never for OnionHeaven machine (first detection)")
                        # Restart caffeinate with the (now-updated) config
                        self.stop_caffeinate()
                        self.start_caffeinate()
                        self.update_menu()

                # OnionHeaven: register or notify online (retries until success)
                if (self.is_ready and not self.is_onionheaven
                        and not self._onionheaven_registration_succeeded
                        and not self._onionheaven_registration_in_flight):
                    # First time or previous registration failed — full registration with keys
                    self._onionheaven_registration_in_flight = True
                    onionheaven.start_registration_thread(self)
                elif (self.is_ready and not self.is_onionheaven
                        and self._onionheaven_registration_succeeded
                        and ready_now and not previous_ready):
                    # Already registered, coming back online (wake/reconnect)
                    onionheaven.start_online_notification_thread(self)

                # OnionHeaven: reclaim address after takeover.
                # If internally ready (checks 1-4) but self-check fails (check 5),
                # OnionHeaven may have taken over our address (serving 302 redirect).
                # Send /online as soon as Tor network is up — don't wait for
                # the self-check, which can't pass until the takeover is released.
                # This also handles fresh launches where a previous session's address
                # was taken over while we were offline.
                # Keep retrying every 60s until we get a positive response.
                if (self._tor_internally_ready and not self.is_ready
                        and not self._onionheaven_reclaim_succeeded
                        and not self._onionheaven_reclaim_in_flight
                        and (time.time() - self._onionheaven_reclaim_last_attempt) > 60):
                    self._onionheaven_reclaim_in_flight = True
                    self._onionheaven_reclaim_last_attempt = time.time()
                    self.log("Internally ready but self-check failing — sending /online to reclaim address")
                    onionheaven.start_online_notification_thread(self)

                # Check if WordPress setup is needed (first-run guard)
                if self._wp_installed is not True and self.proxy_server:
                    wp_installed = self.check_wp_installed()
                    if wp_installed:
                        was_waiting = (self._wp_installed is False)
                        self._wp_installed = True
                        if was_waiting:
                            # Setup just completed — start Tor
                            self.log("Setup complete — starting Tor")
                            threading.Thread(
                                target=lambda: subprocess.run([self.launcher_script, "start-tor"]),
                                daemon=True
                            ).start()
                    elif wp_installed is False and not self._setup_page_opened:
                        # WordPress container responded but WP not installed.
                        # Require 5 consecutive "not installed" results before opening
                        # the setup page — the DB may still be warming up.
                        self._wp_not_installed_count += 1
                        if self._wp_not_installed_count >= 5:
                            self._wp_installed = False
                            self._setup_page_opened = True
                            self.log("WordPress not installed — opening setup page")
                            # Dismiss dialogs before opening browser
                            self.dismiss_setup_dialog()
                            self.dismiss_launch_splash()
                            subprocess.run(["open", f"http://localhost:{onion_proxy.PROXY_PORT}/setup"])
                    else:
                        # Reset counter on None (container not ready) or True
                        self._wp_not_installed_count = 0
            else:
                # Log when stopping
                if self.is_running or self.is_ready:
                    self.log("Service stopped")
                    self.last_status_logged = None

                    # Only dismiss setup dialog when actually stopping (not during startup)
                    self.dismiss_setup_dialog()

                # Keep cached address visible even when stopped — it's still valid
                if not self.onion_address or self.onion_address in ["Starting...", "Generating address..."]:
                    self.onion_address = "Not running"
                self.is_ready = False
                self.auto_opened_browser = False  # Reset for next start
                self._wp_installed = None  # Reset for next start
                self._wp_not_installed_count = 0
                self._setup_page_opened = False
                self._was_ready = False
                self._last_bootstrap_pct = 0
                self._bootstrap_stall_count = 0
                self._yellow_since = None
                self.healthcheck_address = None
                self.onionheaven_messages = []
                self._onionheaven_alert_shown = False
                self._onionheaven_checked = False
                self._onionheaven_registration_succeeded = False
                self._onionheaven_registration_in_flight = False
                self._onionheaven_reclaim_succeeded = False
                self._onionheaven_reclaim_in_flight = False
                self._onionheaven_reclaim_last_attempt = 0
                self._tor_internally_ready = False

                # Stop web log capture if running
                if self.web_log_process is not None:
                    self.stop_web_log_capture()

                # Stop caffeinate to allow Mac to sleep
                self.stop_caffeinate()

            # Update menu
            self.update_menu()

        except Exception as e:
            self.log(f"ERROR in check_status: {e}")
            import traceback
            self.log(traceback.format_exc())
        finally:
            self.checking = False

    def update_menu(self):
        """Update menu items based on current state - thread-safe"""
        # Dispatch UI updates to main thread to avoid AppKit threading violations
        def do_update():
            state = self.display_state

            # OnionHeaven alert indicator: show "!" next to icon when messages exist
            if self.onionheaven_messages:
                self.title = "!"
                count = len(self.onionheaven_messages)
                self.onionheaven_alert_item.title = f"OnionHeaven Alerts ({count})"
                self.onionheaven_alert_item.set_callback(self.view_onionheaven_alerts)
                if self.onionheaven_alert_item.title not in self.menu:
                    self.menu.insert_after("Copy Onion Address", self.onionheaven_alert_item)
            else:
                self.title = ""
                if "OnionHeaven Alerts" in self.menu:
                    del self.menu["OnionHeaven Alerts"]
                for key in list(self.menu.keys()):
                    if isinstance(key, str) and key.startswith("OnionHeaven Alerts ("):
                        del self.menu[key]

            # Show/hide clearnet status based on tunnel config and state
            show_clearnet = (state == "available" and self.cloudflare_tunnel_enabled)
            if show_clearnet:
                self.clearnet_status_item.title = "Clearnet: Active (via Cloudflare)"
                self.clearnet_status_item.set_callback(None)
                if self.clearnet_status_item.title not in self.menu:
                    self.menu.insert_after("Copy Onion Address", self.clearnet_status_item)
            else:
                if "Clearnet: Active (via Cloudflare)" in self.menu:
                    del self.menu["Clearnet: Active (via Cloudflare)"]

            if self._quitting:
                return  # Don't update icon/menu during shutdown

            # Show/hide Wayback queue status
            with self._wayback_queue_lock:
                wq_count = len(self._wayback_queue)
            if wq_count > 0:
                self._wayback_queue_item.title = f"Pending Wayback Saves ({wq_count})"
                if self._wayback_queue_item.title not in self.menu:
                    self.menu.insert_after("Copy Onion Address", self._wayback_queue_item)
            else:
                # Remove any existing queue item (title may have changed)
                for key in list(self.menu.keys()):
                    if isinstance(key, str) and key.startswith("Pending Wayback Saves"):
                        del self.menu[key]

            if state == "available":
                self.icon = self.icon_running
                if self.is_onionheaven:
                    self.menu["Starting..."].title = f"OnionHeaven: {self.onion_address}"
                else:
                    self.menu["Starting..."].title = f"Address: {self.onion_address}"
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
                self.menu["Backup..."].set_callback(self.backup)
                self.menu["Restore..."].set_callback(self.restore)
                self.update_browser_menu_title()
                # Purple: browser opens .onion, local site available as secondary
                self.browser_menu_item.set_callback(self.open_tor_browser)
                self.local_site_item.title = f"Open Local Site ({self.local_url})"
                self.local_site_item.set_callback(self.open_local_site)
            elif state in ("starting", "offline", "stuck"):
                if state == "starting":
                    self.icon = self.icon_starting
                    pct = self._last_bootstrap_pct
                    if pct > 0:
                        self.menu["Starting..."].title = f"Status: Connecting to Tor ({pct}%)..."
                    else:
                        self.menu["Starting..."].title = "Status: Starting up, please wait..."
                elif state == "offline":
                    self.icon = self.icon_stopped
                    self.menu["Starting..."].title = "Status: Offline — no internet connection"
                else:  # stuck
                    self.icon = self.icon_starting
                    self.menu["Starting..."].title = "Status: Slow to connect — try Restart"
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
                self.menu["Backup..."].set_callback(self.backup)
                self.menu["Restore..."].set_callback(self.restore)
                # Gray/Yellow: browser opens local site since .onion isn't reachable
                self.browser_menu_item.title = f"Open Local Site ({self.local_url})"
                self.browser_menu_item.set_callback(self.open_local_site)
                self.local_site_item.title = ""
                self.local_site_item.set_callback(None)
            else:
                # Stopped
                self.icon = self.icon_stopped
                if self.onion_address and self.onion_address.endswith('.onion'):
                    self.menu["Starting..."].title = f"Stopped — {self.onion_address}"
                else:
                    self.menu["Starting..."].title = "Status: Stopped"
                self.menu["Start"].set_callback(self.start_service)
                self.menu["Stop"].set_callback(None)
                self.menu["Restart"].set_callback(None)
                self.menu["Backup..."].set_callback(None)
                self.menu["Restore..."].set_callback(None)
                # Stopped: disable browser items
                self.browser_menu_item.set_callback(None)
                self.local_site_item.title = ""
                self.local_site_item.set_callback(None)

        # Execute on main thread
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(do_update)

    def read_healthcheck_address(self):
        """Read the healthcheck .onion address from the tor container."""
        try:
            # First try the cached file written by the launcher
            hc_file = os.path.join(self.app_support, "healthcheck-address")
            if os.path.exists(hc_file):
                with open(hc_file) as f:
                    addr = f.read().strip()
                if addr and addr.endswith('.onion'):
                    self.healthcheck_address = addr
                    self.log(f"Healthcheck address: {addr}")
                    return

            # Fall back to reading from container
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor",
                 "cat", "/var/lib/tor/hidden_service/healthcheck/hostname"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10, env=env
            )
            if result.returncode == 0:
                addr = result.stdout.strip()
                if addr and addr.endswith('.onion'):
                    self.healthcheck_address = addr
                    # Cache for next time
                    try:
                        with open(hc_file, 'w') as f:
                            f.write(addr)
                    except OSError:
                        pass
                    self.log(f"Healthcheck address: {addr}")
        except Exception as e:
            self.log(f"Failed to read healthcheck address: {e}")

    def poll_onionheaven_messages(self):
        """Poll for messages from OnionHeaven via the healthcheck service."""
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

            # List message files in the container
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor",
                 "ls", "/var/lib/tor/healthcheck-messages/"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10, env=env
            )
            if result.returncode != 0 or not result.stdout.strip():
                if self.onionheaven_messages:
                    self.onionheaven_messages = []
                    self._onionheaven_alert_shown = False
                return

            files = result.stdout.strip().split('\n')
            json_files = [f for f in files if f.endswith('.json')]
            if not json_files:
                if self.onionheaven_messages:
                    self.onionheaven_messages = []
                    self._onionheaven_alert_shown = False
                return

            # Read all message files
            messages = []
            for fname in json_files:
                try:
                    r = subprocess.run(
                        [docker_bin, "exec", "onionpress-tor",
                         "cat", f"/var/lib/tor/healthcheck-messages/{fname}"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5, env=env
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        msg = json.loads(r.stdout.strip())
                        messages.append(msg)
                except Exception:
                    continue

            if messages and messages != self.onionheaven_messages:
                self.onionheaven_messages = messages
                if not self._onionheaven_alert_shown:
                    self._onionheaven_alert_shown = True
                    self.log(f"Received {len(messages)} message(s) from OnionHeaven")
                    latest = messages[-1]
                    msg_type = latest.get("type", "unknown")
                    msg_text = latest.get("message", "New message from OnionHeaven")
                    self.log(f"OnionHeaven alert: {msg_type} - {msg_text}")
        except Exception:
            # Don't spam logs — OnionHeaven polling failures are expected when container is starting
            pass

    def view_onionheaven_alerts(self, _):
        """Show OnionHeaven alert messages and offer to dismiss them."""
        if not self.onionheaven_messages:
            rumps.alert("No OnionHeaven alerts.")
            return

        # Build summary of all messages
        lines = []
        for msg in self.onionheaven_messages:
            msg_type = msg.get("type", "unknown").replace("_", " ").title()
            msg_text = msg.get("message", "")
            lines.append(f"[{msg_type}] {msg_text}")
        summary = "\n".join(lines)

        response = rumps.alert(
            title=f"OnionHeaven Alerts ({len(self.onionheaven_messages)})",
            message=summary,
            ok="Dismiss All",
            cancel="Close"
        )

        if response == 1:  # "Dismiss All" clicked
            self.log("Dismissing OnionHeaven alerts")
            self.onionheaven_messages = []
            self._onionheaven_alert_shown = False
            # Delete message files from container
            try:
                docker_bin = os.path.join(self.bin_dir, "docker")
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
                subprocess.run(
                    [docker_bin, "exec", "onionpress-tor",
                     "sh", "-c", "rm -f /var/lib/tor/healthcheck-messages/*.json"],
                    capture_output=True, timeout=10, env=env
                )
            except Exception:
                pass
            self.update_menu()

    def register_wake_notification(self):
        """Register for macOS wake notification to immediately update icon"""
        ws = AppKit.NSWorkspace.sharedWorkspace()
        nc = ws.notificationCenter()
        nc.addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceWillSleepNotification,
            None,
            AppKit.NSOperationQueue.mainQueue(),
            lambda notification: self.handle_sleep())
        nc.addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceDidWakeNotification,
            None,
            AppKit.NSOperationQueue.mainQueue(),
            lambda notification: self.handle_wake())
        # Register for app termination (catches osascript quit / Apple Event quit)
        AppKit.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationWillTerminateNotification,
            None,
            None,  # Deliver on posting thread (main thread)
            lambda notification: self._handle_terminate())
        self.log("Registered for system sleep/wake/terminate notifications")

    def handle_sleep(self):
        """Handle system sleep — notify OnionHeaven and release caffeinate.
        OnionHeaven resists sleep to keep network active."""
        self.log("System going to sleep")
        if not self.is_onionheaven:
            # Notify OnionHeaven before sleeping so it can take over quickly
            if self.is_ready and self._onionheaven_registration_succeeded:
                try:
                    onionheaven.notify_onionheaven_offline(self)
                except Exception:
                    pass
            self.stop_caffeinate()

    def _handle_terminate(self):
        """Handle app termination (osascript quit, Apple Event, etc.).
        Runs synchronously before the app exits to ensure proper cleanup."""
        if self._quitting:
            return  # Already cleaning up via Quit button
        self._quitting = True
        self.log("="*60)
        self.log("APP TERMINATING (Apple Event / osascript quit)")
        self.log("="*60)

        # Notify OnionHeaven before stopping services
        if self._onionheaven_registration_succeeded and not self.is_onionheaven:
            try:
                onionheaven.notify_onionheaven_offline(self)
            except Exception:
                pass

        # Stop services
        try:
            self.log("Stopping services...")
            subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
            self.log("Services stopped")
        except Exception as e:
            self.log(f"Warning: Stop failed: {e}")

        self.stop_caffeinate()
        self.stop_onion_proxy()

        try:
            colima_bin = os.path.join(self.bin_dir, "colima")
            self.log("Stopping Colima VM...")
            env = os.environ.copy()
            env["COLIMA_HOME"] = self.colima_home
            env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
            env["LIMA_INSTANCE"] = "onionpress"
            subprocess.run([colima_bin, "stop"], capture_output=True, timeout=60, env=env)
            self.log("Colima stopped")
        except Exception as e:
            self.log(f"Warning: Colima stop failed: {e}")

        self._remove_pid_file()
        self.log("Cleanup complete")

    def handle_wake(self):
        """Handle system wake — Tor circuits are dead, go yellow immediately"""
        self.log("System wake detected — marking Tor as reconnecting")
        self.startup_time = time.time()  # Reset so "launched in Xs" shows time since wake
        self.start_caffeinate()
        # Reset OnionHeaven check so /online fires when Tor reconnects
        self._onionheaven_checked = False
        self._wordpress_confirmed = False  # Re-verify WordPress once after wake
        if self.is_ready:
            self.is_ready = False
            self._last_bootstrap_pct = 0
            self._bootstrap_stall_count = 0
            self._yellow_since = time.time()
            self.update_menu()
        # SIGHUP Tor so it rebuilds stale circuits immediately
        gen = self._run_generation
        threading.Thread(target=self._sighup_tor, args=(gen,), daemon=True).start()

    def _sighup_tor(self, generation):
        """Send SIGHUP to Tor container to force circuit rebuild after wake.
        If Tor hasn't bootstrapped within 2 minutes, restart the container.
        Bails out if a stop/start changed the generation."""
        if generation != self._run_generation:
            return
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            # Try SIGHUP on PID 1 (works for both C-tor and Arti entrypoint)
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor", "kill", "-HUP", "1"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=10)
            if result.returncode == 0:
                self.log("Sent SIGHUP to Tor/Arti — rebuilding circuits")
            else:
                self.log(f"Failed to SIGHUP Tor: {result.stderr.strip()}")
        except Exception as e:
            self.log(f"Failed to SIGHUP Tor: {e}")

        # Wait up to 2 minutes for Tor to bootstrap; only restart if unhealthy
        time.sleep(120)
        if generation != self._run_generation:
            return  # User stopped or restarted — don't touch containers
        if not self.is_ready and not self._stopping and not self._quitting and self._tor_container_unhealthy():
            self.log("Tor unhealthy 2min after SIGHUP — restarting container")
            try:
                docker_bin = os.path.join(self.bin_dir, "docker")
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
                subprocess.run(
                    [docker_bin, "restart", "onionpress-tor"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=30)
                self.log("Tor container restarted")
            except Exception as e:
                self.log(f"Failed to restart Tor container: {e}")

    def _tor_container_unhealthy(self):
        """Check if the tor container shows signs of actual trouble.
        Returns True if logs indicate broken state (restart will help),
        False if Arti is healthy but just waiting for propagation."""
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            result = subprocess.run(
                [docker_bin, "logs", "onionpress-tor", "--tail", "50"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=10)
            logs = (result.stderr or "") + (result.stdout or "")
            # Signs of trouble that a restart can fix
            sick_patterns = [
                "No usable guards",
                "Too many preemptive onion service circuits failed",
                "Rejected 60/60 as down",
                "Could not connect rendezvous circuit",
            ]
            # Signs of health — Arti is working, just waiting for propagation.
            # "reuploading descriptor" is NOT included because it can coexist
            # with a circuit failure spiral — Arti can still upload descriptors
            # even when it can't build rendezvous circuits.
            healthy_patterns = [
                "Sufficiently bootstrapped",
            ]
            has_sick = any(p in logs for p in sick_patterns)
            has_healthy = any(p in logs for p in healthy_patterns)
            if has_sick:
                # Any sick pattern means trouble — restart will help
                self.log("Tor health check: unhealthy (circuit/guard failures)")
                return True
            if has_healthy:
                self.log("Tor health check: healthy, waiting for propagation")
                return False
            # No recognizable patterns — assume unhealthy if we've been waiting
            self.log("Tor health check: no clear signals, restarting as precaution")
            return True
        except Exception as e:
            self.log(f"Tor health check failed: {e}")
            return True  # Can't check — restart as fallback

    def _auto_restart_tor(self):
        """Auto-restart the tor container when onion service fails to come up.
        Arti sometimes fails to establish introduction points on first boot;
        a restart fixes it because the Tor directory is cached."""
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            subprocess.run(
                [docker_bin, "restart", "onionpress-tor"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=30)
            self.log("Tor container restarted — retrying onion service setup")
        except Exception as e:
            self.log(f"Failed to auto-restart Tor container: {e}")

    def start_status_checker(self):
        """Start background thread to check status periodically"""
        def checker():
            while True:
                if self._port_conflict:
                    time.sleep(30)
                    continue
                self.check_status()
                # Adaptive polling based on display state
                state = self.display_state
                if state == "available":
                    time.sleep(30)  # Check every 30 seconds when operational
                elif state == "offline":
                    time.sleep(10)  # Check every 10 seconds when offline (detect recovery)
                else:
                    time.sleep(5)   # Check every 5 seconds during startup/stuck

        thread = threading.Thread(target=checker, daemon=True)
        thread.start()

    @property
    def local_url(self):
        """The local URL for accessing WordPress."""
        return f"http://localhost:{self.wp_port}"

    @rumps.clicked("Copy Onion Address")
    def copy_address(self, _):
        """Copy onion address to clipboard"""
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            subprocess.run(
                ["pbcopy"],
                input=self.onion_address.encode(),
                check=True
            )
        else:
            rumps.alert("Onion address not available yet. Please wait for the service to start.")

    def open_local_site(self, _):
        """Open the local WordPress site in the default browser"""
        url = self.local_url
        subprocess.run(["open", url])
        self.log(f"Opened local site: {url}")

    def monitor_tor_browser_install(self):
        """Monitor for Tor Browser installation and offer to open site when detected"""
        if self.monitoring_tor_install:
            return  # Already monitoring

        self.monitoring_tor_install = True
        self.log("Starting Tor Browser installation monitor")

        def check_for_tor():
            tor_browser_path = "/Applications/Tor Browser.app"
            timeout = 600  # 10 minutes
            check_interval = 3  # Check every 3 seconds
            elapsed = 0

            while elapsed < timeout and self.monitoring_tor_install:
                time.sleep(check_interval)
                elapsed += check_interval

                # Verify the app is in /Applications and is a proper app bundle
                if os.path.exists(tor_browser_path) and os.path.isdir(tor_browser_path):
                    # Check it's actually in /Applications (not on a volume)
                    real_path = os.path.realpath(tor_browser_path)
                    if not real_path.startswith("/Applications/"):
                        continue  # It's a symlink or on a volume, keep waiting

                    # Verify it's a proper app bundle with executable
                    executable_path = os.path.join(tor_browser_path, "Contents", "MacOS", "firefox")
                    if not os.path.exists(executable_path):
                        continue  # Not fully installed yet

                    self.log("Tor Browser detected in Applications!")
                    self.monitoring_tor_install = False

                    # Dismiss setup dialog before showing browser ready dialog
                    self.dismiss_setup_dialog()

                    # Show dialog asking if they want to open the site
                    address = self.onion_address
                    try:
                        button_index = self.show_native_alert(
                            title="OnionPress",
                            message=f"Tor Browser is now installed!\n\nWould you like to open your site?\n\n{address}",
                            buttons=["Open Site", "Later"],
                            default_button=0,
                            style="informational"
                        )

                        if button_index == 0:  # Open Site
                            url = f"http://{address}"
                            # Use full path to ensure we open the one in Applications
                            subprocess.run(["open", "-a", tor_browser_path, url])
                            self.log(f"Opened site in Tor Browser: {url}")
                    except Exception as e:
                        self.log(f"Error showing Tor Browser ready dialog: {e}")
                    return

            # Timeout reached
            self.monitoring_tor_install = False
            self.log("Tor Browser installation monitor timed out")

        threading.Thread(target=check_for_tor, daemon=True).start()

    def monitor_brave_install(self):
        """Monitor for Brave Browser installation and offer to open site when detected"""
        if self.monitoring_tor_install:  # Reuse the same flag since we only monitor one at a time
            return  # Already monitoring

        self.monitoring_tor_install = True
        self.log("Starting Brave Browser installation monitor")

        def check_for_brave():
            brave_browser_path = "/Applications/Brave Browser.app"
            timeout = 600  # 10 minutes
            check_interval = 3  # Check every 3 seconds
            elapsed = 0

            while elapsed < timeout and self.monitoring_tor_install:
                time.sleep(check_interval)
                elapsed += check_interval

                # Verify the app is in /Applications and is a proper app bundle
                if os.path.exists(brave_browser_path) and os.path.isdir(brave_browser_path):
                    # Check it's actually in /Applications (not on a volume)
                    real_path = os.path.realpath(brave_browser_path)
                    if not real_path.startswith("/Applications/"):
                        continue  # It's a symlink or on a volume, keep waiting

                    # Verify it's a proper app bundle with executable
                    executable_path = os.path.join(brave_browser_path, "Contents", "MacOS", "Brave Browser")
                    if not os.path.exists(executable_path):
                        continue  # Not fully installed yet

                    self.log("Brave Browser detected in Applications!")
                    self.monitoring_tor_install = False

                    # Dismiss setup dialog before showing browser ready dialog
                    self.dismiss_setup_dialog()

                    # Show dialog asking if they want to open the site
                    address = self.onion_address
                    try:
                        button_index = self.show_native_alert(
                            title="OnionPress",
                            message=f"Brave Browser is now installed!\n\nWould you like to open your site?\n\n{address}",
                            buttons=["Open Site", "Later"],
                            default_button=0,
                            style="informational"
                        )

                        if button_index == 0:  # Open Site
                            url = f"http://{address}"
                            # Launch Brave in Tor mode using executable with --tor flag
                            brave_executable = os.path.join(brave_browser_path, "Contents", "MacOS", "Brave Browser")
                            subprocess.run([brave_executable, "--tor", url])
                            self.log(f"Opened site in Brave Browser (Tor mode): {url}")
                    except Exception as e:
                        self.log(f"Error showing Brave Browser ready dialog: {e}")
                    return

            # Timeout reached
            self.monitoring_tor_install = False
            self.log("Brave Browser installation monitor timed out")

        threading.Thread(target=check_for_brave, daemon=True).start()

    # Browsers we trust for open -a / osascript activate
    ALLOWED_BROWSERS = {"Firefox", "Google Chrome", "Brave Browser", "Microsoft Edge", "Safari"}

    def extension_connected_recently(self):
        """Check if a browser extension is actively connected right now.

        Returns the browser app name (e.g. "Firefox") if connected in the
        last 10 seconds, or None if not. Only returns names from ALLOWED_BROWSERS.
        """
        marker = os.path.join(self.app_support, "extension-connected")
        try:
            if os.path.exists(marker):
                with open(marker, 'r') as f:
                    data = json.loads(f.read().strip())
                if (time.time() - data["timestamp"]) < 10:
                    browser = data.get("browser")
                    if browser in self.ALLOWED_BROWSERS:
                        return browser
        except Exception:
            pass
        return None

    def update_browser_menu_title(self):
        """Update the browser menu item title based on which browser is available"""
        tor_browser_path = "/Applications/Tor Browser.app"
        brave_browser_path = "/Applications/Brave Browser.app"

        if os.path.exists(tor_browser_path):
            self.browser_menu_item.title = "Open in Tor Browser"
        else:
            ext_browser = self.extension_connected_recently()
            if ext_browser:
                self.browser_menu_item.title = f"Open in {ext_browser}"
            elif os.path.exists(brave_browser_path):
                self.browser_menu_item.title = "Open in Brave Browser"
            else:
                self.browser_menu_item.title = "Open in Browser"

    def open_tor_browser(self, _):
        """Open the onion address in the best available browser"""
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            tor_browser_path = "/Applications/Tor Browser.app"
            brave_browser_path = "/Applications/Brave Browser.app"
            url = f"http://{self.onion_address}"

            ext_browser = self.extension_connected_recently()
            if ext_browser:
                subprocess.run(["open", "-a", ext_browser, url])
                self.log(f"Opened {url} in {ext_browser} (extension)")
            elif os.path.exists(brave_browser_path):
                brave_executable = os.path.join(brave_browser_path, "Contents", "MacOS", "Brave Browser")
                subprocess.run([brave_executable, "--tor", url])
                self.log(f"Opened {url} in Brave Browser (Tor mode)")
            elif os.path.exists(tor_browser_path):
                subprocess.run(["open", "-a", "Tor Browser", url])
                self.log(f"Opened {url} in Tor Browser")
            else:
                self.show_browser_install_dialog()
        else:
            rumps.alert("Onion address not available yet. Please wait for the service to start.")

    def show_browser_install_dialog(self):
        """Show dialog with browser options based on what's installed."""
        # Detect which extension-compatible browsers are installed
        extension_browsers = {
            "Firefox": "/Applications/Firefox.app",
            "Google Chrome": "/Applications/Google Chrome.app",
            "Brave Browser": "/Applications/Brave Browser.app",
            "Microsoft Edge": "/Applications/Microsoft Edge.app",
        }
        installed = [name for name, path in extension_browsers.items()
                     if os.path.exists(path)]

        address = self.onion_address or ""
        try:
            if installed:
                # Has a compatible browser — suggest installing the extension
                browsers_str = ", ".join(installed)
                button_index = self.show_native_alert(
                    title="OnionPress",
                    message=f"Your site is ready!\n\n{address}\n\nInstall the OnionPress extension for {browsers_str} to browse .onion sites.\n\nOr download Tor Browser for a dedicated solution.",
                    buttons=["Install Extension", "Download Tor Browser", "Later"],
                    default_button=0,
                    cancel_button=2,
                    style="informational"
                )
                if button_index == 0:
                    subprocess.run(["open", "https://github.com/brewsterkahle/onionpress/releases/latest"])
                elif button_index == 1:
                    subprocess.run(["open", "https://www.torproject.org/download/"])
                    self.monitor_tor_browser_install()
            else:
                # Safari-only user — don't mention extension
                button_index = self.show_native_alert(
                    title="OnionPress",
                    message=f"Your site is ready!\n\n{address}\n\nTo visit .onion sites, download Tor Browser or Brave Browser (both are free).",
                    buttons=["Download Tor Browser", "Download Brave Browser", "Later"],
                    default_button=0,
                    cancel_button=2,
                    style="informational"
                )
                if button_index == 0:
                    subprocess.run(["open", "https://www.torproject.org/download/"])
                    self.monitor_tor_browser_install()
                elif button_index == 1:
                    subprocess.run(["open", "https://brave.com/download/"])
                    self.monitor_brave_install()
        except Exception as e:
            self.log(f"Browser dialog failed: {e}")

    def auto_open_browser(self):
        """Automatically open a browser when service becomes ready"""
        try:
            self._auto_open_browser_inner()
        except Exception as e:
            self.log(f"ERROR in auto_open_browser: {e}")
            import traceback
            self.log(traceback.format_exc())

    def _auto_open_browser_inner(self):
        """Inner implementation of auto_open_browser"""
        # Wait until the onion service is actually reachable before opening
        # the browser. Poll via docker exec into the tor container (the same
        # path the launcher uses) instead of a fixed sleep.
        if not self.onion_address or self.onion_address in ["Starting...", "Not running", "Generating address..."]:
            self.log(f"auto_open_browser: skipping, onion_address={self.onion_address!r}")
            return

        self.log("Waiting for onion service to become reachable before opening browser...")

        # Test the actual .onion address through the independent tor-client
        # container. Unlike onionpress-tor (which hosts the service and can
        # resolve its own .onion locally), tor-client must discover the address
        # through the real Tor network — giving a true reachability test.
        onion_url = f"http://{self.onion_address}/"
        docker_bin = os.path.join(self.bin_dir, "docker")
        docker_env = os.environ.copy()
        docker_env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        docker_env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

        reachable = False
        for attempt in range(30):  # Up to 90s (30 x 3s)
            try:
                result = subprocess.run(
                    [docker_bin, "exec", "onionpress-tor-client",
                     "curl", "-s", "--socks5-hostname", "127.0.0.1:9050",
                     "--max-time", "10", "-o", "/dev/null", "-w", "%{http_code}",
                     onion_url],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15, env=docker_env
                )
                if result.returncode == 0 and result.stdout.strip() in ["200", "301"]:
                    reachable = True
                    self.log(f"Onion service reachable via tor-client after {(attempt + 1) * 3}s")
                    break
            except Exception:
                pass
            time.sleep(3)

        if not reachable:
            self.log("WARNING: Onion service not reachable after 90s, opening browser anyway")

        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            tor_browser_path = "/Applications/Tor Browser.app"
            brave_browser_path = "/Applications/Brave Browser.app"
            url = f"http://{self.onion_address}"

            if os.path.exists(tor_browser_path):
                self.log(f"Auto-opening Tor Browser: {url}")
                subprocess.run(["open", "-a", "Tor Browser", url])
            else:
                # Wait for the onion proxy to start (it runs in the monitoring
                # loop which continues in parallel now)
                for i in range(15):
                    if self.proxy_server is not None:
                        break
                    time.sleep(1)

                # Wait up to 5 more seconds for a browser extension to register
                ext_browser = None
                for i in range(5):
                    ext_browser = self.extension_connected_recently()
                    if ext_browser:
                        break
                    self.log(f"Waiting for extension registration... ({i+1}/5)")
                    time.sleep(1)
                if ext_browser:
                    self.log(f"Auto-opening {ext_browser} (extension detected): {url}")
                    # Open the browser first (without the URL) so the extension
                    # background script starts and can poll /status to set up
                    # SOCKS routing BEFORE we navigate to the .onion address.
                    subprocess.run(["open", "-a", ext_browser])
                    # Wait for extension to poll /status and set up SOCKS routing.
                    # Extension polls every 2s at startup, every 60s thereafter.
                    marker = os.path.join(self.app_support, "extension-connected")
                    for i in range(30):
                        try:
                            with open(marker, 'r') as f:
                                data = json.loads(f.read().strip())
                            if (time.time() - data["timestamp"]) < 5:
                                self.log(f"Extension active after {i+1}s, opening .onion URL")
                                break
                        except Exception:
                            pass
                        time.sleep(1)
                    else:
                        self.log("Extension did not poll within 30s, opening .onion URL anyway")
                    # Now open the .onion URL — extension should have SOCKS routing active
                    subprocess.run(["open", "-a", ext_browser, url])
                    subprocess.run(["osascript", "-e", f'tell application "{ext_browser}" to activate'])
                elif os.path.exists(brave_browser_path):
                    self.log(f"Auto-opening Brave Browser (Tor mode): {url}")
                    brave_executable = os.path.join(brave_browser_path, "Contents", "MacOS", "Brave Browser")
                    subprocess.run([brave_executable, "--tor", url])
                else:
                    self.log("No Tor-capable browser found - showing options dialog")
                    self.dismiss_setup_dialog()
                    self.dismiss_launch_splash()
                    self.show_browser_install_dialog()

    def validate_address_prefix(self, prefix):
        """Validate a address prefix string.

        Returns:
            (valid, error_message, suggestion) tuple.
            suggestion is a corrected prefix string (or "" if no fix is possible).
        """
        if not prefix:
            return (True, "", "")

        # Build a suggested fix: lowercase, strip invalid chars, truncate to 5
        suggested = re.sub(r'[^a-z2-7]', '', prefix.lower())[:5]

        if len(prefix) > 5 and re.match(r'^[a-z2-7]+$', prefix):
            # Valid chars but too long — suggest truncated version
            return (False,
                    f"Address prefix \"{prefix}\" is too long and would take "
                    f"hours or days to generate ({len(prefix)} characters).\n\n"
                    f"Maximum length is 5 characters.",
                    suggested)

        if not re.match(r'^[a-z2-7]+$', prefix):
            # Has invalid characters — explain what's wrong and suggest a fix
            has_upper = any(c.isupper() for c in prefix)
            has_digits_089 = any(c in '0189' for c in prefix)

            msg = f"Address prefix \"{prefix}\" contains invalid characters.\n\n"
            msg += "Onion addresses use base32 encoding:\n"
            msg += "  Allowed letters:  a-z\n"
            msg += "  Allowed numbers:  2, 3, 4, 5, 6, 7\n"
            msg += "  NOT allowed:  0, 1, 8, 9\n"

            if has_upper:
                msg += f"\nUppercase letters will be lowercased."
            if has_digits_089:
                bad_digits = sorted(set(c for c in prefix if c in '0189'))
                msg += f"\nDigits {', '.join(bad_digits)} are not valid in base32 and will be removed."

            return (False, msg, suggested)

        return (True, "", prefix)

    def check_address_prefix_change(self):
        """Check if ADDRESS_PREFIX has changed and handle regeneration.

        Called from a background thread before starting the launcher.
        Returns True if startup should proceed, False to abort.
        """
        # Read configured prefix (fall back to old VANITY_PREFIX for migration)
        prefix = self._read_config_value("ADDRESS_PREFIX", "").strip()
        if not prefix:
            prefix = self._read_config_value("VANITY_PREFIX", "op2").strip()
        if not prefix:
            prefix = "op2"

        # Validate prefix
        valid, error_msg, suggestion = self.validate_address_prefix(prefix)
        if not valid:
            self.log(f"Invalid ADDRESS_PREFIX: {prefix}")

            # Try to determine the current working prefix from the onion address
            current_prefix = "op2"  # fallback default
            try:
                docker_bin = os.path.join(self.bin_dir, "docker")
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
                result = subprocess.run(
                    [docker_bin, "run", "--rm", "-v", "onionpress-tor-keys:/keys",
                     "alpine", "cat", "/keys/wordpress/hostname"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=15
                )
                hostname = result.stdout.strip().replace(".onion", "")
                if hostname:
                    # Extract the prefix from the current address
                    current_prefix = hostname[:len(prefix)] if len(hostname) >= len(prefix) else hostname[:3]
            except Exception:
                pass

            # Build button list: Use suggestion (if different), Revert, Edit Again
            # NSAlert supports max 3 buttons well
            buttons = []
            if suggestion and suggestion != current_prefix:
                buttons.append(f"Use \"{suggestion}\"")
            buttons.append(f"Revert to \"{current_prefix}\"")
            buttons.append("Edit Again")
            revert_idx = len(buttons) - 2
            edit_idx = len(buttons) - 1

            button_index = self.show_native_alert(
                "Invalid Address Prefix",
                error_msg + (f"\n\nSuggested prefix: \"{suggestion}\"" if suggestion and suggestion != current_prefix else ""),
                buttons=buttons,
                default_button=0,
                cancel_button=edit_idx,
                style="warning"
            )

            if suggestion and suggestion != current_prefix and button_index == 0:
                self.log(f"User accepted suggested prefix: {suggestion}")
                self.write_config_value("ADDRESS_PREFIX", suggestion)
                prefix = suggestion
            elif button_index == revert_idx:
                self.log(f"User reverted to prefix: {current_prefix}")
                self.write_config_value("ADDRESS_PREFIX", current_prefix)
                prefix = current_prefix
            else:
                # Open config for editing and bring TextEdit to front
                self.log("User chose to edit config — opening TextEdit")
                config_file = os.path.join(self.app_support, "config")
                subprocess.Popen(["open", "-a", "TextEdit", config_file])
                subprocess.Popen(["osascript", "-e", 'tell application "TextEdit" to activate'])
                # Show follow-up dialog — when dismissed, retry start
                self.show_native_alert(
                    "Edit Settings",
                    "Edit the config file in TextEdit, then save it (⌘S).\n\nClick OK when you're done to restart.",
                    buttons=["OK"],
                    style="informational"
                )
                self.log("User finished editing — retrying start")
                # Re-read config and retry from the top
                return self.check_address_prefix_change()

        self.log(f"Prefix validation passed, checking current hostname (prefix={prefix})")

        # Skip prefix check if a key import is pending — the launcher will handle it
        pending_file = os.path.join(self.app_support, ".import-key-pending")
        if os.path.exists(pending_file):
            self.log("Key import pending — skipping prefix check (launcher will swap volume)")
            return True

        # Try to get current hostname from tor-keys volume
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            result = subprocess.run(
                [docker_bin, "run", "--rm", "-v", "onionpress-tor-keys:/keys",
                 "alpine", "cat", "/keys/wordpress/hostname"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=15
            )
            current_hostname = result.stdout.strip()
        except Exception as e:
            self.log(f"Could not read current hostname (likely first run): {e}")
            return True  # No existing volume, proceed normally

        if not current_hostname or not current_hostname.endswith(".onion"):
            self.log("No existing onion address found, proceeding with first run")
            return True

        # Check if current hostname already matches the prefix
        hostname_base = current_hostname.replace(".onion", "")
        if hostname_base.startswith(prefix):
            self.log(f"Address prefix '{prefix}' matches current address {current_hostname}")
            return True

        # Mismatch detected — determine old prefix for display
        old_prefix = hostname_base[:len(prefix)] if len(hostname_base) >= len(prefix) else hostname_base[:3]
        self.log(f"Address prefix changed: current address starts with '{old_prefix}', config says '{prefix}'")

        # Show confirmation dialog with time estimates
        time_estimates = (
            "Estimated generation time:\n"
            "  2 characters:  < 1 second\n"
            "  3 characters:  < 1 second\n"
            "  4 characters:  5-30 seconds\n"
            "  5 characters:  10-30 minutes"
        )

        message = (
            f"Your address prefix has changed from what was used to generate "
            f"your current onion address.\n\n"
            f"Current address:\n{current_hostname}\n\n"
            f"New prefix: \"{prefix}\"\n\n"
            f"Changing will generate a NEW onion address.\n"
            f"Your current address will stop working permanently.\n\n"
            f"{time_estimates}"
        )

        button_index = self.show_native_alert(
            "Change Onion Address?",
            message,
            buttons=["Change Address", "Keep Current Address"],
            default_button=1,
            cancel_button=1,
            style="warning"
        )

        if button_index == 0:
            # User confirmed — delete old keys so launcher regenerates
            self.log("User confirmed address prefix change — deleting old keys")

            # Unregister old address from OnionHeaven (it will never come back)
            try:
                onionheaven.unregister_from_onionheaven(self, content_address=current_hostname)
            except Exception as e:
                self.log(f"OnionHeaven unregister failed (continuing): {e}")

            try:
                docker_bin = os.path.join(self.bin_dir, "docker")
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

                # Delete vanity-keys directory
                vanity_dir = os.path.join(self.app_support, "shared", "vanity-keys")
                if os.path.exists(vanity_dir):
                    import shutil
                    shutil.rmtree(vanity_dir)
                    self.log(f"Deleted vanity-keys directory: {vanity_dir}")

                # Delete docker volume
                subprocess.run(
                    [docker_bin, "volume", "rm", "onionpress-tor-keys"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=15
                )
                self.log("Deleted onionpress-tor-keys volume")

                # Clear cached onion address
                cached_addr_file = os.path.join(self.app_support, "onion_address")
                if os.path.exists(cached_addr_file):
                    os.remove(cached_addr_file)
                    self.log("Cleared cached onion address")

            except Exception as e:
                self.log(f"Error cleaning up old keys: {e}")
                self.show_native_alert(
                    "Error",
                    f"Failed to remove old onion keys:\n\n{e}\n\nPlease try again or manually delete ~/.onionpress/shared/vanity-keys/ and run: docker volume rm onionpress-tor-keys",
                    buttons=["OK"],
                    style="critical"
                )
                return False

            return True
        else:
            # User cancelled — don't start
            self.log("User chose to keep current address — aborting start")
            return False

    @rumps.clicked("Start")
    def start_service(self, _):
        """Start the WordPress + Tor service"""
        self._stopping = False  # Clear in case Stop was hit previously
        self.menu["Starting..."].title = "Status: Starting..."

        def start():
            # Check if this is first run (no docker images yet)
            first_run = False
            try:
                result = subprocess.run(
                    ["docker", "images", "--format", "{{.Repository}}"],
                    capture_output=True,
                    text=True, encoding='utf-8', errors='replace',
                    timeout=5
                )
                images = result.stdout.strip().split('\n')
                # First run if we don't have wordpress/mysql/tor images
                if not any('wordpress' in img for img in images):
                    first_run = True
            except Exception:
                pass

            # First run: launch splash is already showing — just run setup
            if first_run:
                self.log("First run detected - starting installation")
                threading.Thread(target=self._run_first_time_setup, daemon=True).start()
                return

            # Not first run: check if address prefix changed before starting
            if not self.check_address_prefix_change():
                self.log("Start aborted due to address prefix issue")
                self.menu["Starting..."].title = "Status: Stopped"
                return

            # Start the service normally
            subprocess.run([self.launcher_script, "start"])

            # Poll until WordPress is responding (replaces fixed sleep)
            max_wait = 60
            waited = 0
            while waited < max_wait:
                if self.check_wordpress_health(log_result=False):
                    self.log(f"WordPress responding after {waited}s")
                    break
                time.sleep(2)
                waited += 2

            self.check_status()

            # Start caffeinate to prevent sleep while service runs
            self.start_caffeinate()

        threading.Thread(target=start, daemon=True).start()

    def _run_first_time_setup(self):
        """Run first-time setup: launcher start, pull images, then wait for ready."""
        try:
            self.log("Starting Colima VM and containers...")
            subprocess.run([self.launcher_script, "start"])
        except Exception as e:
            self.log(f"Error in _run_first_time_setup: {e}")

        # Monitor image downloads (logs progress to onionpress.log)
        self.monitor_image_downloads()

        # Poll until WordPress is responding
        max_wait = 60
        waited = 0
        while waited < max_wait:
            if self.check_wordpress_health(log_result=False):
                self.log(f"WordPress responding after {waited}s")
                break
            time.sleep(2)
            waited += 2

        self.check_status()
        self.start_caffeinate()

    @rumps.clicked("Stop")
    def stop_service(self, _):
        """Stop the WordPress + Tor service"""
        self._stopping = True  # Prevent health monitor from auto-restarting
        self._run_generation += 1  # Cancel any pending SIGHUP threads
        self.menu["Starting..."].title = "Status: Stopping..."

        def stop():
            # Notify OnionHeaven before stopping services
            if self._onionheaven_registration_succeeded and not self.is_onionheaven:
                try:
                    onionheaven.notify_onionheaven_offline(self)
                except Exception:
                    pass

            subprocess.run([self.launcher_script, "stop"])
            time.sleep(1)
            self.check_status()

            # Stop background processes
            self.stop_web_log_capture()
            self.stop_caffeinate()
            self.stop_onion_proxy()
            self._stopping = False

        threading.Thread(target=stop, daemon=True).start()

    @rumps.clicked("Restart")
    def restart_service(self, _):
        """Restart the WordPress + Tor service"""
        self._stopping = False  # Clear in case Stop was hit previously
        self.menu["Starting..."].title = "Status: Restarting..."
        self.icon = self.icon_starting  # Change icon to indicate restarting

        def restart():
            # Mark as not ready during restart
            self.is_ready = False
            self.is_running = False
            self._was_ready = False
            self._last_bootstrap_pct = 0
            self._bootstrap_stall_count = 0
            self._yellow_since = None
            self.auto_opened_browser = False  # Re-open browser after restart

            # Check if address prefix changed before restarting
            if not self.check_address_prefix_change():
                self.log("Restart aborted due to address prefix issue")
                self.menu["Starting..."].title = "Status: Stopped"
                self.icon = self.icon_stopped
                return

            # Run restart command
            subprocess.run([self.launcher_script, "restart"])

            # Poll until WordPress is responding (replaces fixed sleep)
            max_wait = 60
            waited = 0
            while waited < max_wait:
                if self.check_wordpress_health(log_result=False):
                    self.log(f"WordPress responding after restart ({waited}s)")
                    break
                time.sleep(2)
                waited += 2

            # Check status after restart
            self.check_status()

        threading.Thread(target=restart, daemon=True).start()

    @rumps.clicked("View Logs")
    def view_logs(self, _):
        """Open logs in built-in log viewer"""
        log_file = os.path.join(self.app_support, "onionpress.log")
        if os.path.exists(log_file):
            _LogViewerWindow.show_for_file(log_file, "OnionPress Log")
        else:
            rumps.alert("No logs available yet")

    @rumps.clicked("View Web Usage Log")
    def view_web_log(self, _):
        """Open WordPress access log in built-in log viewer"""
        if not self.is_running:
            rumps.alert("Service not running. Please start the service first.")
            return

        web_log_file = os.path.join(self.app_support, "wordpress-visitors.log")

        # Ensure the log file exists
        if not os.path.exists(web_log_file):
            # Create it and wait a moment for logs to populate
            open(web_log_file, 'a').close()
            time.sleep(1)

        # Open in built-in log viewer (filtered log excludes health check pings)
        _LogViewerWindow.show_for_file(web_log_file, "OnionPress Web Usage Log")

    def get_version(self):
        """Get version from Info.plist"""
        try:
            with open(self.info_plist, 'rb') as f:
                plist = plistlib.load(f)
                return plist.get('CFBundleShortVersionString', 'Unknown')
        except Exception:
            return 'Unknown'

    def read_config_value(self, key, default=""):
        """Read a value from the config file"""
        config_file = os.path.join(self.app_support, "config")
        if not os.path.exists(config_file):
            return default
        try:
            with open(config_file, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{key}="):
                        return line.split('=', 1)[1]
        except Exception:
            pass
        return default

    def write_config_value(self, key, value):
        """Write a value to the config file"""
        config_file = os.path.join(self.app_support, "config")

        # Create default config if it doesn't exist
        if not os.path.exists(config_file):
            config_template = os.path.join(self.resources_dir, "config-template.txt")
            if os.path.exists(config_template):
                subprocess.run(["cp", config_template, config_file])

        # Read all lines
        lines = []
        if os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()

        # Update or add the key
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={value}\n"
                found = True
                break

        if not found:
            lines.append(f"{key}={value}\n")

        # Write back
        with open(config_file, 'w', encoding='utf-8') as f:
            f.writelines(lines)

    # -- Settings help text (from config-template comments) --
    _SETTINGS_HELP = {
        "ADDRESS_PREFIX": (
            "Onion Address Prefix\n\n"
            "Customise the beginning of your .onion address.\n"
            "Default: \"op2\" (generates addresses like op2xxxxxxxxxxxxx.onion)\n\n"
            "Only base32 characters allowed (a-z, 2-7). Numbers 0, 1, 8, 9 are not valid.\n"
            "Maximum 5 characters. Longer prefixes take exponentially longer to generate:\n"
            "  2 chars: < 1 second\n"
            "  3 chars: < 1 second\n"
            "  4 chars: 5-30 seconds\n"
            "  5 chars: 10-30 minutes"
        ),
        "VM_MEMORY": (
            "Virtual Machine Memory (GB)\n\n"
            "RAM allocated to the Linux VM that runs WordPress, Tor, and MariaDB.\n"
            "1 GB is sufficient for normal use. Increase if you run many plugins "
            "or experience out-of-memory issues.\n\n"
            "Requires restart to take effect."
        ),
        "VM_CPU": (
            "Virtual Machine CPUs\n\n"
            "Number of CPU cores allocated to the Linux VM.\n"
            "2 is sufficient for normal use. OnionHeaven mode automatically "
            "sets this to 3/4 of your Mac's cores.\n\n"
            "Requires restart to take effect."
        ),
        "PREVENT_SLEEP": (
            "Sleep Prevention Mode\n\n"
            "Controls whether OnionPress keeps your Mac awake.\n\n"
            "Normal: Mac sleeps as usual. Your site goes offline when sleeping.\n\n"
            "On AC Power: Stay awake when plugged in, sleep on battery. "
            "Good balance of uptime and battery life.\n\n"
            "Never: Mac never idle-sleeps while OnionPress runs. "
            "Best for always-on servers (OnionHeaven machines).\n\n"
            "Display sleep is not affected \u2014 the screen can still turn off."
        ),
        "LAUNCH_ON_LOGIN": (
            "Launch on Login\n\n"
            "Automatically start OnionPress when you log in to macOS.\n"
            "Installs a LaunchAgent that runs OnionPress at login."
        ),
        "UPDATE_ON_LAUNCH": (
            "Update Docker Images on Launch\n\n"
            "Automatically check for updated WordPress, MariaDB, and Tor container "
            "images when the app launches. Ensures you have the latest security patches."
        ),
        "INSTALL_IA_PLUGIN": (
            "Internet Archive Wayback Machine Link Fixer Plugin\n\n"
            "Automatically installs and activates the IA Link Fixer plugin, which:\n"
            "  - Scans posts for outbound links\n"
            "  - Creates archived versions in the Wayback Machine\n"
            "  - Redirects to archived versions when links break\n"
            "  - Archives your own posts on every update"
        ),
        "REGISTER_WITH_ONIONHEAVEN": (
            "Register with OnionHeaven\n\n"
            "Registers your site with OnionHeaven so it can redirect page "
            "requests to the Wayback Machine as a fallback when your Mac is offline."
        ),
        "ONIONHEAVEN_ADDRESS": (
            "OnionHeaven Hub Address\n\n"
            "The .onion address of the OnionHeaven hub your site registers with. "
            "When your site goes offline, the hub redirects visitors to the "
            "Wayback Machine copy.\n\n"
            "Default: oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
        ),
        "TOR_IMPL": (
            "Tor Implementation\n\n"
            "Choose which Tor implementation runs your onion services.\n\n"
            "Arti: Tor Project's modern Rust implementation (default). "
            "Native arm64 on Apple Silicon.\n\n"
            "C Tor: The classic C implementation. Faster onion service releases "
            "(sends DESTROY cells to intro relays). Available via apt-get.\n\n"
            "Keys are automatically converted between formats when switching.\n"
            "Requires restart to take effect."
        ),
        "CLOUDFLARE_TUNNEL_TOKEN": (
            "Cloudflare Tunnel (Clearnet Access)\n\n"
            "Expose your WordPress site on the regular internet via Cloudflare Tunnel.\n\n"
            "PRIVACY NOTE: This reveals your Mac's IP address to Cloudflare. "
            "Your site is no longer anonymous.\n\n"
            "Setup:\n"
            "1. Create a free Cloudflare account and add your domain\n"
            "2. Go to Zero Trust > Networks > Tunnels > Create a tunnel\n"
            "3. Set the tunnel service to http://wordpress:80\n"
            "4. Copy the tunnel token and paste it below\n"
            "5. Restart OnionPress"
        ),
    }

    # Consequence text shown in the hazards confirmation dialog
    _SETTINGS_CONSEQUENCES = {
        "ADDRESS_PREFIX": (
            "Your current .onion address will stop working. A new address "
            "will be generated. Existing links or bookmarks will break."
        ),
        "VM_MEMORY": (
            "The VM will be resized on next restart. "
            "Brief downtime expected while the VM restarts."
        ),
        "VM_CPU": (
            "The VM will be resized on next restart. "
            "Brief downtime expected while the VM restarts."
        ),
        "PREVENT_SLEEP": {
            "normal": "Mac will sleep normally. Site goes offline when sleeping.",
            "on-battery": "Mac stays awake on AC power. Sleeps normally on battery.",
            "never": "Mac will never idle-sleep while OnionPress runs. Best for always-on servers.",
        },
        "LAUNCH_ON_LOGIN": {
            "yes": "OnionPress will start automatically on login.",
            "no": "OnionPress will no longer auto-start.",
        },
        "UPDATE_ON_LAUNCH": {
            "yes": "Docker images will update automatically on launch.",
            "no": "Automatic updates disabled.",
        },
        "INSTALL_IA_PLUGIN": {
            "yes": "Internet Archive plugin will be installed.",
            "no": "Internet Archive plugin will not be auto-installed.",
        },
        "REGISTER_WITH_ONIONHEAVEN": {
            "yes": "Site will register with OnionHeaven.",
            "no": "OnionHeaven registration disabled. Wayback fallback won't work.",
        },
        "TOR_IMPL": {
            "arti": "Tor will run using Arti (Rust). Requires restart.",
            "tor": "Tor will run using C Tor. Faster releases. Requires restart.",
        },
        "CLOUDFLARE_TUNNEL_TOKEN": {
            "set": (
                "Your site will be exposed on the clearnet via Cloudflare. "
                "Your Mac's IP will be visible to Cloudflare."
            ),
            "cleared": "Clearnet access will be disabled.",
        },
    }

    def _show_setting_help(self, key):
        """Show help alert for a setting."""
        rumps.alert(title="Help", message=self._SETTINGS_HELP.get(key, ""))

    @rumps.clicked("Settings...")
    def open_settings(self, _):
        """Show GUI settings dialog"""
        config_file = os.path.join(self.app_support, "config")

        # Create default config if it doesn't exist
        if not os.path.exists(config_file):
            config_template = os.path.join(self.parent_resources_dir, "config-template.txt")
            if os.path.exists(config_template):
                subprocess.run(["cp", config_template, config_file])

        if not os.path.exists(config_file):
            rumps.alert("Settings file not found")
            return

        # -- Read current values --
        settings_keys = [
            ("ADDRESS_PREFIX", "op2"),
            ("VM_MEMORY", "1"),
            ("VM_CPU", "2"),
            ("PREVENT_SLEEP", "normal"),
            ("LAUNCH_ON_LOGIN", "yes"),
            ("UPDATE_ON_LAUNCH", "yes"),
            ("INSTALL_IA_PLUGIN", "yes"),
            ("REGISTER_WITH_ONIONHEAVEN", "yes"),
            ("ONIONHEAVEN_ADDRESS", "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"),
            ("TOR_IMPL", "arti"),
            ("CLOUDFLARE_TUNNEL_TOKEN", ""),
        ]
        old_values = {}
        for key, default in settings_keys:
            old_values[key] = self._read_config_value(key, default)
        # Normalize legacy yes/no for PREVENT_SLEEP
        ps = old_values.get("PREVENT_SLEEP", "normal").lower()
        if ps == "yes":
            old_values["PREVENT_SLEEP"] = "on-battery"
        elif ps == "no":
            old_values["PREVENT_SLEEP"] = "normal"
        elif ps not in ("normal", "on-battery", "never"):
            old_values["PREVENT_SLEEP"] = "normal"

        icon_path = os.path.join(self.resources_dir, "app-icon.png")

        # Layout constants
        field_w = 300
        row_h = 30
        label_w = 170
        input_x = 175
        input_w = 100
        help_x = 280
        help_w = 25
        container_h = 10 * row_h + 10

        def _alert(title, message):
            """Show an alert with the OnionPress icon."""
            a = AppKit.NSAlert.alloc().init()
            a.setMessageText_(title)
            a.setInformativeText_(message)
            if os.path.exists(icon_path):
                img = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if img:
                    a.setIcon_(img)
            a.runModal()

        # Create help button target (shared across dialog rebuilds)
        help_target = _HelpButtonTarget.alloc().init()
        help_keys = [
            "ADDRESS_PREFIX", "VM_MEMORY", "VM_CPU", "PREVENT_SLEEP",
            "LAUNCH_ON_LOGIN", "UPDATE_ON_LAUNCH", "INSTALL_IA_PLUGIN",
            "REGISTER_WITH_ONIONHEAVEN", "ONIONHEAVEN_ADDRESS",
            "TOR_IMPL", "CLOUDFLARE_TUNNEL_TOKEN",
        ]
        help_target._help_texts = {
            i: self._SETTINGS_HELP[k] for i, k in enumerate(help_keys)
        }
        help_target._icon_path = icon_path
        self._help_target = help_target  # prevent GC during modal

        # Current form values — starts from config, updated on validation failure
        form_values = dict(old_values)

        while True:
            # -- Build settings form dialog --
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("OnionPress Settings")
            alert.setInformativeText_("Change settings below. Click (?) for help on any setting.")

            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    alert.setIcon_(icon)

            container = AppKit.NSView.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, field_w, container_h))

            fields = {}
            tag_counter = [0]

            def _make_help_btn(y):
                btn = AppKit.NSButton.alloc().initWithFrame_(
                    AppKit.NSMakeRect(help_x, y, help_w, 24))
                btn.setBezelStyle_(9)  # NSBezelStyleHelpButton
                btn.setTag_(tag_counter[0])
                btn.setTarget_(help_target)
                btn.setAction_(help_target.helpClicked_)
                container.addSubview_(btn)
                tag_counter[0] += 1

            def add_text_row(y, label_text, key, value):
                label = AppKit.NSTextField.labelWithString_(label_text)
                label.setFrame_(AppKit.NSMakeRect(0, y + 3, label_w, 18))
                container.addSubview_(label)

                field = AppKit.NSTextField.alloc().initWithFrame_(
                    AppKit.NSMakeRect(input_x, y, input_w, 24))
                field.setStringValue_(str(value))
                container.addSubview_(field)
                fields[key] = field

                _make_help_btn(y)
                return field

            def add_check_row(y, label_text, key, value):
                cb = AppKit.NSButton.alloc().initWithFrame_(
                    AppKit.NSMakeRect(0, y, help_x - 5, 24))
                cb.setButtonType_(AppKit.NSButtonTypeSwitch)
                cb.setTitle_(label_text)
                if value.lower() == "yes":
                    cb.setState_(AppKit.NSControlStateValueOn)
                else:
                    cb.setState_(AppKit.NSControlStateValueOff)
                container.addSubview_(cb)
                fields[key] = cb

                _make_help_btn(y)
                return cb

            def add_popup_row(y, label_text, key, value, options):
                label = AppKit.NSTextField.labelWithString_(label_text)
                label.setFrame_(AppKit.NSMakeRect(0, y + 3, label_w, 18))
                container.addSubview_(label)

                popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
                    AppKit.NSMakeRect(input_x, y, input_w, 24), False)
                for title, val in options:
                    popup.addItemWithTitle_(title)
                # Select current value
                for i, (_, val) in enumerate(options):
                    if val == value:
                        popup.selectItemAtIndex_(i)
                        break
                container.addSubview_(popup)
                fields[key] = popup

                _make_help_btn(y)
                return popup

            y = container_h - row_h
            prefix_field = add_text_row(y, "Address Prefix:", "ADDRESS_PREFIX", form_values["ADDRESS_PREFIX"])
            y -= row_h
            add_text_row(y, "VM Memory (GB):", "VM_MEMORY", form_values["VM_MEMORY"])
            y -= row_h
            add_text_row(y, "VM CPUs:", "VM_CPU", form_values["VM_CPU"])
            y -= row_h
            # Normalize legacy yes/no for the popup display
            sleep_val = form_values["PREVENT_SLEEP"].lower()
            if sleep_val == "yes":
                sleep_val = "on-battery"
            elif sleep_val == "no":
                sleep_val = "normal"
            elif sleep_val not in ("normal", "on-battery", "never"):
                sleep_val = "normal"
            add_popup_row(y, "Sleep Prevention:", "PREVENT_SLEEP", sleep_val, [
                ("Normal", "normal"),
                ("On AC Power", "on-battery"),
                ("Never Sleep", "never"),
            ])
            y -= row_h
            add_check_row(y, "Launch on Login", "LAUNCH_ON_LOGIN", form_values["LAUNCH_ON_LOGIN"])
            y -= row_h
            add_check_row(y, "Update Docker on Launch", "UPDATE_ON_LAUNCH", form_values["UPDATE_ON_LAUNCH"])
            y -= row_h
            add_check_row(y, "Install IA Plugin", "INSTALL_IA_PLUGIN", form_values["INSTALL_IA_PLUGIN"])
            y -= row_h
            add_check_row(y, "Register with OnionHeaven (advanced)", "REGISTER_WITH_ONIONHEAVEN", form_values["REGISTER_WITH_ONIONHEAVEN"])
            y -= row_h
            oh_addr_field = add_text_row(y, "OnionHeaven Hub (advanced):", "ONIONHEAVEN_ADDRESS", form_values["ONIONHEAVEN_ADDRESS"])
            oh_addr_field.setPlaceholderString_("oheavenfhb...onion")
            oh_addr_field.setFrame_(AppKit.NSMakeRect(input_x, oh_addr_field.frame().origin.y, input_w, 24))
            y -= row_h
            tor_impl_val = form_values.get("TOR_IMPL", "arti").lower()
            if tor_impl_val not in ("arti", "tor"):
                tor_impl_val = "arti"
            add_popup_row(y, "Tor Implementation (advanced):", "TOR_IMPL", tor_impl_val, [
                ("Arti (default)", "arti"),
                ("C Tor", "tor"),
            ])
            y -= row_h
            cf_field = add_text_row(y, "Cloudflare Token (optional):", "CLOUDFLARE_TUNNEL_TOKEN", form_values["CLOUDFLARE_TUNNEL_TOKEN"])
            cf_field.setPlaceholderString_("paste tunnel token")
            cf_field.setFrame_(AppKit.NSMakeRect(input_x, cf_field.frame().origin.y, input_w, 24))

            alert.setAccessoryView_(container)

            save_btn = alert.addButtonWithTitle_("Save")
            cancel_btn = alert.addButtonWithTitle_("Cancel")
            cancel_btn.setKeyEquivalent_("\r")
            save_btn.setKeyEquivalent_("")

            alert.window().setInitialFirstResponder_(prefix_field)

            response = alert.runModal()
            if response != 1000:  # Not "Save"
                return

            # -- Collect new values from form --
            new_values = {}
            sleep_options_map = ["normal", "on-battery", "never"]
            tor_impl_options_map = ["arti", "tor"]
            for key in [k for k, _ in settings_keys]:
                widget = fields[key]
                if key == "PREVENT_SLEEP":
                    idx = widget.indexOfSelectedItem()
                    new_values[key] = sleep_options_map[idx] if 0 <= idx < len(sleep_options_map) else "normal"
                elif key == "TOR_IMPL":
                    idx = widget.indexOfSelectedItem()
                    new_values[key] = tor_impl_options_map[idx] if 0 <= idx < len(tor_impl_options_map) else "arti"
                elif key in ("LAUNCH_ON_LOGIN", "UPDATE_ON_LAUNCH",
                             "INSTALL_IA_PLUGIN", "REGISTER_WITH_ONIONHEAVEN"):
                    new_values[key] = "yes" if widget.state() == AppKit.NSControlStateValueOn else "no"
                else:
                    new_values[key] = widget.stringValue().strip()

            # -- Validate prefix --
            prefix = new_values["ADDRESS_PREFIX"]
            if prefix and not re.match(r'^[a-z2-7]+$', prefix):
                _alert("Invalid Address Prefix",
                       "Only lowercase base32 characters allowed (a-z, 2-7).\n"
                       "Numbers 0, 1, 8, 9 are not valid.")
                form_values = new_values
                form_values["ADDRESS_PREFIX"] = old_values["ADDRESS_PREFIX"]
                continue
            if len(prefix) > 5:
                _alert("Invalid Address Prefix",
                       "Address prefix must be at most 5 characters.")
                form_values = new_values
                form_values["ADDRESS_PREFIX"] = old_values["ADDRESS_PREFIX"]
                continue

            # -- Validate VM memory --
            try:
                mem = int(new_values["VM_MEMORY"])
                if mem < 1:
                    raise ValueError
            except ValueError:
                _alert("Invalid VM Memory",
                       "VM memory must be a whole number of at least 1 GB.")
                form_values = new_values
                form_values["VM_MEMORY"] = old_values["VM_MEMORY"]
                continue

            # -- Validate VM CPUs --
            try:
                cpus = int(new_values["VM_CPU"])
                if cpus < 1:
                    raise ValueError
            except ValueError:
                _alert("Invalid VM CPUs",
                       "VM CPUs must be a whole number of at least 1.")
                form_values = new_values
                form_values["VM_CPU"] = old_values["VM_CPU"]
                continue

            # -- Validate OnionHeaven address --
            oh_addr = new_values.get("ONIONHEAVEN_ADDRESS", "").strip()
            if oh_addr and oh_addr != old_values.get("ONIONHEAVEN_ADDRESS", ""):
                self.log(f"Validating OnionHeaven address: {oh_addr}")
                try:
                    script = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "MacOS", "onionpress")
                    result = subprocess.run(
                        [script, "validate-oh-address", oh_addr],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=90
                    )
                    import json as _json
                    vr = _json.loads(result.stdout.strip()) if result.stdout.strip() else {}
                    status = vr.get("status", "")
                    message = vr.get("message", "")

                    if status == "invalid_format" or status == "self":
                        _alert("Invalid OnionHeaven Address", message)
                        form_values = new_values
                        form_values["ONIONHEAVEN_ADDRESS"] = old_values["ONIONHEAVEN_ADDRESS"]
                        continue
                    elif status == "site":
                        resp = rumps.alert(
                            title="Not an OnionHeaven Hub",
                            message=f"{message}\n\nUse it anyway?",
                            ok="Use Anyway",
                            cancel="Cancel"
                        )
                        if resp != 1:
                            form_values = new_values
                            form_values["ONIONHEAVEN_ADDRESS"] = old_values["ONIONHEAVEN_ADDRESS"]
                            continue
                        self.log(f"User accepted non-hub address: {oh_addr}")
                    elif status == "unreachable":
                        resp = rumps.alert(
                            title="Address Unreachable",
                            message=f"{message}\n\nUse it anyway?",
                            ok="Use Anyway",
                            cancel="Cancel"
                        )
                        if resp != 1:
                            form_values = new_values
                            form_values["ONIONHEAVEN_ADDRESS"] = old_values["ONIONHEAVEN_ADDRESS"]
                            continue
                        self.log(f"User accepted unreachable address: {oh_addr}")
                    else:
                        self.log(f"OnionHeaven hub validated: {oh_addr}")
                except Exception as e:
                    self.log(f"OnionHeaven address validation error: {e}")

            # Validation passed
            break

        # -- Find changed settings --
        changes = []
        for key, _ in settings_keys:
            if new_values[key] != old_values[key]:
                changes.append(key)

        if not changes:
            _alert("Settings", "No changes.")
            return

        # -- Dialog 2: Hazards confirmation --
        change_lines = []
        for key in changes:
            old_v = old_values[key]
            new_v = new_values[key]

            # Human-readable label
            labels = {
                "ADDRESS_PREFIX": "Address Prefix",
                "VM_MEMORY": "VM Memory (GB)",
                "VM_CPU": "VM CPUs",
                "PREVENT_SLEEP": "Sleep Prevention",
                "LAUNCH_ON_LOGIN": "Launch on Login",
                "UPDATE_ON_LAUNCH": "Update Docker on Launch",
                "INSTALL_IA_PLUGIN": "Install IA Plugin",
                "REGISTER_WITH_ONIONHEAVEN": "Register with OnionHeaven",
                "ONIONHEAVEN_ADDRESS": "OnionHeaven Hub",
                "TOR_IMPL": "Tor Implementation",
                "CLOUDFLARE_TUNNEL_TOKEN": "Cloudflare Token",
            }
            label = labels.get(key, key)

            # Display values (truncate long tokens)
            disp_old = old_v if len(old_v) <= 20 else old_v[:17] + "..."
            disp_new = new_v if len(new_v) <= 20 else new_v[:17] + "..."
            if not disp_old:
                disp_old = "(empty)"
            if not disp_new:
                disp_new = "(empty)"

            line = f"- {label}: {disp_old} \u2192 {disp_new}"

            # Look up consequence text
            cons = self._SETTINGS_CONSEQUENCES.get(key)
            if isinstance(cons, dict):
                if key == "CLOUDFLARE_TUNNEL_TOKEN":
                    cons_text = cons.get("set") if new_v else cons.get("cleared")
                else:
                    cons_text = cons.get(new_v, "")
            elif isinstance(cons, str):
                cons_text = cons
            else:
                cons_text = ""

            if cons_text:
                line += f"\n  \u26a0 {cons_text}"
            change_lines.append(line)

        hazard_msg = "The following settings will change:\n\n" + "\n\n".join(change_lines)

        hazard = AppKit.NSAlert.alloc().init()
        hazard.setMessageText_("Confirm Changes")
        hazard.setInformativeText_(hazard_msg)

        if os.path.exists(icon_path):
            icon2 = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon2:
                hazard.setIcon_(icon2)

        apply_btn = hazard.addButtonWithTitle_("Apply")
        cancel2 = hazard.addButtonWithTitle_("Cancel")
        cancel2.setKeyEquivalent_("\r")
        apply_btn.setKeyEquivalent_("")

        resp2 = hazard.runModal()
        if resp2 != 1000:  # Not "Apply"
            return

        # -- Write changed values --
        for key in changes:
            self.write_config_value(key, new_values[key])

        # Apply sleep mode change immediately (no restart needed)
        if "PREVENT_SLEEP" in changes:
            self.stop_caffeinate()
            self.start_caffeinate()

        # Apply login item change immediately
        if "LAUNCH_ON_LOGIN" in changes:
            if new_values["LAUNCH_ON_LOGIN"] == "yes":
                self.add_login_item()
            else:
                self.remove_login_item()

        self.log(f"Settings updated: {', '.join(changes)}")
        saved = AppKit.NSAlert.alloc().init()
        saved.setMessageText_("Settings Saved")
        saved.setInformativeText_(
            "Settings saved. Restart OnionPress from the menu bar "
            "for changes to take effect.")
        if os.path.exists(icon_path):
            icon3 = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon3:
                saved.setIcon_(icon3)
        saved.runModal()

    @rumps.clicked("Backup...")
    def backup(self, _):
        """Create a full backup of OnionPress (Tor keys, database, wp-content)"""
        # Show credentials dialog using AppKit accessory view
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Backup OnionPress")
        alert.setInformativeText_(
            "Enter your WordPress administrator credentials.\n"
            "The password will be used to encrypt the backup.")

        icon_path = os.path.join(self.resources_dir, "app-icon.png")
        if os.path.exists(icon_path):
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon:
                alert.setIcon_(icon)

        # Build accessory view with username and password fields
        container = AppKit.NSView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 300, 70))

        user_label = AppKit.NSTextField.labelWithString_("Username:")
        user_label.setFrame_(AppKit.NSMakeRect(0, 48, 80, 18))
        container.addSubview_(user_label)

        user_field = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(85, 44, 210, 24))
        user_field.setStringValue_("admin")
        container.addSubview_(user_field)

        pass_label = AppKit.NSTextField.labelWithString_("Password:")
        pass_label.setFrame_(AppKit.NSMakeRect(0, 18, 80, 18))
        container.addSubview_(pass_label)

        pass_field = AppKit.NSSecureTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(85, 14, 210, 24))
        container.addSubview_(pass_field)

        alert.setAccessoryView_(container)
        alert.addButtonWithTitle_("Backup").setKeyEquivalent_("\r")
        alert.addButtonWithTitle_("Cancel").setKeyEquivalent_("\x1b")

        # Make username field first responder
        alert.window().setInitialFirstResponder_(user_field)
        user_field.setNextKeyView_(pass_field)

        response = alert.runModal()
        if response != 1000:  # Not "Backup"
            return

        username = user_field.stringValue().strip()
        password = pass_field.stringValue()

        if not username or not password:
            rumps.alert(title="Missing Credentials",
                        message="Both username and password are required.")
            return

        # Verify credentials
        self.log("Backup: verifying credentials...")
        ok, err = backup_manager.verify_wp_admin(username, password)
        if not ok:
            self.log(f"Backup: credential verification failed: {err}")
            rumps.alert(title="Verification Failed", message=err)
            return

        # Show NSSavePanel for output location
        panel = AppKit.NSSavePanel.savePanel()
        panel.setTitle_("Save Backup")
        panel.setNameFieldStringValue_(
            backup_manager.backup_filename(self.onion_address, username))
        panel.setDirectoryURL_(
            AppKit.NSURL.fileURLWithPath_(os.path.expanduser("~/Downloads/")))
        panel.setAllowedContentTypes_([
            AppKit.UTType.typeWithFilenameExtension_("zip")])

        if panel.runModal() != 1:  # NSModalResponseOK
            return

        output_path = panel.URL().path()

        # Show progress window (stored on self to prevent garbage collection)
        self._progress_window = _BackupProgressWindow("Backing Up OnionPress")
        self._progress_window.show()

        def do_backup():
            pw = self._progress_window
            try:
                def log_and_update(msg):
                    self.log(msg)
                    display = msg.replace("Backup: ", "") if msg.startswith("Backup: ") else msg
                    _main_thread(lambda: pw.update(display))

                backup_manager.create_backup(
                    self.onion_address, username, password,
                    output_path, self.version, log_and_update)

                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                msg = f"Backup saved to {os.path.basename(output_path)} ({size_mb:.1f} MB)"
                _main_thread(lambda: pw.finish(msg))
            except Exception as e:
                self.log(f"Backup failed: {e}")
                _main_thread(lambda: pw.finish(f"Backup failed: {e}"))

        threading.Thread(target=do_backup, daemon=True).start()

    @rumps.clicked("Restore...")
    def restore(self, _):
        """Restore OnionPress from a backup zip"""
        # File picker for .zip
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setTitle_("Select OnionPress Backup")
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedContentTypes_([
            AppKit.UTType.typeWithFilenameExtension_("zip")])

        if panel.runModal() != 1:  # NSModalResponseOK
            return

        zip_path = panel.URL().path()

        # Try to extract username from backup filename
        # Format: OnionPress-<addr>-<username>-<date>.zip
        zip_name = os.path.basename(zip_path)
        backup_user = None
        if zip_name.startswith("OnionPress-") and zip_name.endswith(".zip"):
            parts = zip_name[len("OnionPress-"):-len(".zip")].split("-")
            if len(parts) >= 3:
                # parts[0] = addr prefix, parts[1] = username, rest = date
                backup_user = parts[1]

        # Prompt for password
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Enter Backup Password")
        if backup_user:
            alert.setInformativeText_(
                f"Enter the password of '{backup_user}' that was used "
                f"when this backup was created.")
        else:
            alert.setInformativeText_(
                "Enter the password that was used when this backup was created.")

        icon_path = os.path.join(self.resources_dir, "app-icon.png")
        if os.path.exists(icon_path):
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon:
                alert.setIcon_(icon)

        pass_field = AppKit.NSSecureTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 300, 24))
        alert.setAccessoryView_(pass_field)
        alert.addButtonWithTitle_("Continue").setKeyEquivalent_("\r")
        alert.addButtonWithTitle_("Cancel").setKeyEquivalent_("\x1b")
        alert.window().setInitialFirstResponder_(pass_field)

        response = alert.runModal()
        if response != 1000:
            return

        password = pass_field.stringValue()
        if not password:
            rumps.alert(title="No Password", message="A password is required.")
            return

        # Validate zip by reading metadata
        try:
            metadata = backup_manager.read_backup_metadata(zip_path, password)
        except ValueError as e:
            rumps.alert(title="Invalid Backup", message=str(e))
            return
        except Exception as e:
            self.log(f"Restore: failed to read backup metadata: {e}")
            rumps.alert(title="Invalid Backup",
                        message=f"Could not read backup: {e}")
            return

        # Show confirmation with backup details
        addr = metadata.get('onion_address', 'unknown')
        date = metadata.get('backup_date', 'unknown')
        user = metadata.get('username', 'unknown')
        ver = metadata.get('onionpress_version', 'unknown')

        button_index = self.show_native_alert(
            title="Confirm Restore",
            message=(
                f"You are about to restore from this backup:\n\n"
                f"Onion address: {addr}\n"
                f"Backup date: {date}\n"
                f"Username: {user}\n"
                f"OnionPress version: {ver}\n\n"
                f"WARNING: This will overwrite your current site, "
                f"database, and onion address. This cannot be undone."),
            buttons=["Cancel", "Restore"],
            default_button=0,
            cancel_button=0,
            style="critical"
        )

        if button_index != 1:
            return

        # Show progress window (stored on self to prevent garbage collection)
        self._progress_window = _BackupProgressWindow("Restoring OnionPress")
        self._progress_window.show()

        def do_restore():
            pw = self._progress_window
            try:
                def log_and_update(msg):
                    self.log(msg)
                    display = msg.replace("Restore: ", "") if msg.startswith("Restore: ") else msg
                    _main_thread(lambda: pw.update(display))

                backup_manager.restore_from_backup(
                    zip_path, password, log_and_update)

                restored_addr = metadata.get('onion_address', addr)

                # Build summary of what was restored and what will happen
                notes = [f"Onion address: {restored_addr}"]

                # Check if onionheaven mode was restored
                onionheaven_addr = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
                if restored_addr == onionheaven_addr:
                    cur_mem = self._read_config_value("VM_MEMORY", "1")
                    try:
                        cur_mem_int = int(cur_mem)
                    except ValueError:
                        cur_mem_int = 1
                    if cur_mem_int < 5:
                        notes.append("OnionHeaven detected — VM memory will increase to 5 GB on relaunch.")
                    else:
                        notes.append(f"OnionHeaven detected — VM memory: {cur_mem} GB.")

                notes.append("\nPlease quit and relaunch OnionPress for the restore to take full effect.")

                summary = "Site restored successfully.\n\n" + "\n".join(notes)
                _main_thread(lambda: pw.finish(summary))
            except Exception as e:
                self.log(f"Restore failed: {e}")
                _main_thread(lambda: pw.finish(f"Restore failed: {e}"))

        threading.Thread(target=do_restore, daemon=True).start()

    def update_docker_images(self, show_notifications=True):
        """Update Docker images (WordPress, MariaDB, Tor)"""
        try:
            self.log("Checking for Docker image updates...")

            docker_bin = os.path.join(self.bin_dir, "docker")
            docker_compose_file = os.path.join(self.parent_resources_dir, "docker", "docker-compose.yml")

            # Set up environment
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

            # Pull latest images
            self.log("Pulling latest Docker images...")
            result = subprocess.run(
                [docker_bin, "compose", "-f", docker_compose_file, "pull"],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=300,  # 5 minute timeout
                env=env
            )

            if result.returncode == 0:
                self.log("Docker images updated successfully")
                if "Downloaded" in result.stdout or "Pulled" in result.stdout:
                    return True
                else:
                    return False
            else:
                self.log(f"Failed to update Docker images: {result.stderr}")
                return False

        except Exception as e:
            self.log(f"Error updating Docker images: {e}")
            return False

    @rumps.clicked("Check for Updates...")
    def check_for_updates(self, _):
        """Check GitHub for newer versions and update Docker images"""
        # Check for app updates
        app_update_available = False
        try:
            # Fetch latest release from GitHub using curl to avoid permission prompts
            # --cacert needed because py2app bundle can't find CA certs (curl exit 77)
            url = "https://api.github.com/repos/brewsterkahle/onionpress/releases/latest"
            result = subprocess.run(
                ["curl", "-s", "--cacert", "/etc/ssl/cert.pem",
                 "-H", "User-Agent: onionpress", "--max-time", "10", url],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=15
            )

            if result.returncode == 0:
                data = json.loads(result.stdout)
                latest_version = data.get('tag_name', '').lstrip('v')
                current_version = self.version
                self.log(f"Update check: current={current_version}, latest={latest_version}")

                if latest_version and parse_version(latest_version) > parse_version(current_version):
                    app_update_available = True
                    response = rumps.alert(
                        title="App Update Available",
                        message=f"A new version of OnionPress is available!\n\nCurrent: v{current_version}\nLatest: v{latest_version}\n\nWould you like to download it?",
                        ok="Download Update",
                        cancel="Later"
                    )
                    if response == 1:  # OK clicked
                        release_url = data.get('html_url', 'https://github.com/brewsterkahle/onionpress/releases/latest')
                        subprocess.run(["open", release_url])
            else:
                self.log(f"Update check curl failed: exit={result.returncode} stderr={result.stderr.strip()}")
        except Exception as e:
            self.log(f"Update check failed: {e}")
            import traceback
            self.log(traceback.format_exc())
            rumps.alert(
                title="Update Check Failed",
                message=f"Could not check for app updates.\n\nPlease visit:\nhttps://github.com/brewsterkahle/onionpress/releases"
            )

        # Check for Docker image updates
        threading.Thread(target=self._check_docker_updates_async, args=(app_update_available,), daemon=True).start()

    def _check_docker_updates_async(self, app_update_available):
        """Check for Docker updates in background thread"""
        images_updated = self.update_docker_images(show_notifications=True)

        # Show final summary if no app update was available.
        if not app_update_available and not images_updated:
            version = self.version
            self.show_native_alert(
                "No Updates Available",
                f"You're running the latest version (v{version})\nAll container images are up to date."
            )

    def show_setup_dialog(self):
        """Show a persistent setup dialog during first run that stays until service is ready"""
        try:
            # Dismiss any existing dialog first
            self.dismiss_setup_dialog()

            # Create and show dialog on main thread, storing reference for programmatic dismissal
            def create_and_show():
                try:
                    alert = AppKit.NSAlert.alloc().init()
                    alert.setMessageText_("OnionPress Setup")
                    alert.setInformativeText_("Setting up OnionPress for first use...\n\n• Downloading container images\n• Configuring Tor onion service\n• Starting WordPress\n\nThis may take 2-5 minutes depending on your internet speed.\n\nThis window will close automatically to set up your WordPress.")
                    alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

                    btn_dismiss = alert.addButtonWithTitle_("Dismiss")
                    btn_dismiss.setKeyEquivalent_("\r")
                    btn_cancel = alert.addButtonWithTitle_("Cancel Setup")
                    btn_cancel.setKeyEquivalent_("\x1b")

                    # Set app icon
                    icon_path = os.path.join(self.resources_dir, "app-icon.png")
                    if os.path.exists(icon_path):
                        icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                        if icon:
                            alert.setIcon_(icon)

                    # Store reference so dismiss_setup_dialog can close it
                    self.setup_alert = alert

                    # runModal blocks until button click or abortModal
                    response = alert.runModal()

                    # Close the alert window
                    alert.window().close()
                    self.setup_alert = None

                    # NSModalResponseAbort = -1001 (from abortModal call)
                    if response == AppKit.NSModalResponseAbort:
                        self.log("Setup dialog auto-dismissed (service ready)")
                    else:
                        button_index = response - 1000
                        if button_index == 1:
                            self.log("User cancelled setup - stopping services")
                            subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
                        elif button_index == 0:
                            self.log("User dismissed setup dialog")

                    self.setup_dialog_showing = False
                except Exception as e:
                    self.log(f"Error in setup dialog: {e}")
                    self.setup_dialog_showing = False
                    self.setup_alert = None

            self.setup_dialog_showing = True
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(create_and_show)
            self.log("Setup dialog shown (native NSAlert)")
        except Exception as e:
            self.log(f"Error showing setup dialog: {e}")
            self.setup_dialog_showing = False
            self.log("Setup dialog fallback - dialog failed to show")

    def dismiss_setup_dialog(self):
        """Dismiss the setup dialog if it's showing (native NSAlert)"""
        if self.setup_dialog_showing:
            self.setup_dialog_showing = False
            self.log("Setup dialog marked for dismissal")
            try:
                if self.setup_alert:
                    AppKit.NSApp.abortModal()
                    self.log("Setup dialog dismissed programmatically")
            except Exception as e:
                self.log(f"Error dismissing setup dialog: {e}")

    def monitor_image_downloads(self):
        """Monitor Docker image downloads and log progress."""
        images_to_check = {
            'wordpress': False,
            'mariadb': False,
            'tor': False
        }

        self.log("Monitoring image downloads...")

        # Check for images every 3 seconds for up to 10 minutes
        for i in range(200):
            try:
                result = subprocess.run(
                    ["docker", "images", "--format", "{{.Repository}}"],
                    capture_output=True,
                    text=True, encoding='utf-8', errors='replace',
                    timeout=5
                )
                current_images = result.stdout.strip().split('\n')

                for image_name in images_to_check:
                    if not images_to_check[image_name]:
                        if any(image_name in img for img in current_images):
                            images_to_check[image_name] = True
                            self.log(f"Image downloaded: {image_name}")

                if all(images_to_check.values()):
                    self.log("All images downloaded")
                    break

            except Exception as e:
                self.log(f"Error checking images: {e}")

            time.sleep(3)

    @rumps.clicked("About OnionPress")
    def show_about(self, _):
        """Show about dialog"""
        about_text = f"""OnionPress v{self.version}

Run your own website from your Mac. Just Works. Free, forever.
WordPress + Tor Onion Service

Features:
• Full WordPress that you own and run
• Internet Archive's Wayback Machine integration
• Tor Onion Service with an address you own, forever
• Requires visitors to use Tor or Brave browsers
• Privacy-first design
• Free and open source

Created by the Internet Archive
License: AGPL v3"""

        github_url = "https://github.com/brewsterkahle/onionpress"
        link_label = "GitHub: github.com/brewsterkahle/onionpress"

        def show_dialog():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("About OnionPress")
            alert.setInformativeText_(about_text)
            alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

            btn = alert.addButtonWithTitle_("OK")
            btn.setKeyEquivalent_("\r")

            # Set app icon if available
            icon_path = os.path.join(self.resources_dir, "app-icon.png")
            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    alert.setIcon_(icon)

            # Create clickable GitHub link as accessory view
            link_field = AppKit.NSTextField.labelWithString_("")
            link_field.setSelectable_(True)
            link_field.setAllowsEditingTextAttributes_(True)
            link_field.setBordered_(False)
            link_field.setDrawsBackground_(False)

            # Build attributed string with clickable link
            attr_str = AppKit.NSMutableAttributedString.alloc().initWithString_(link_label)
            url = AppKit.NSURL.URLWithString_(github_url)
            link_range = AppKit.NSMakeRange(len("GitHub: "), len(link_label) - len("GitHub: "))
            attr_str.addAttribute_value_range_(AppKit.NSLinkAttributeName, url, link_range)
            font = AppKit.NSFont.systemFontOfSize_(AppKit.NSFont.smallSystemFontSize())
            full_range = AppKit.NSMakeRange(0, len(link_label))
            attr_str.addAttribute_value_range_(AppKit.NSFontAttributeName, font, full_range)

            link_field.setAttributedStringValue_(attr_str)
            link_field.sizeToFit()
            alert.setAccessoryView_(link_field)

            alert.runModal()

        if AppKit.NSThread.isMainThread():
            show_dialog()
        else:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(show_dialog)

    @rumps.clicked("Uninstall...")
    def uninstall(self, _):
        """Uninstall OnionPress with mandatory backup prompt"""
        # Step 1: Show critical warning about data loss (native NSAlert - no permissions)
        button_index = self.show_native_alert(
            title="Uninstall Warning",
            message="CRITICAL WARNING\n\nUninstalling will PERMANENTLY DELETE:\n\u2022 Your onion address and private key\n\u2022 All WordPress content and data\n\u2022 Database and configuration\n\nYour site CANNOT BE RECOVERED unless you have a backup.\n\nDo you want to create a backup before uninstalling?",
            buttons=["Cancel", "No, Delete Everything", "Yes, Backup First"],
            default_button=2,
            cancel_button=0,
            style="critical"
        )

        if button_index == 0:  # Cancel
            return

        if button_index == 2:  # Yes, Backup First
            self.log("User chose to backup before uninstall")
            if self.is_running:
                self.backup(None)
            else:
                rumps.alert(
                    title="Service Not Running",
                    message="Cannot create a backup while service is stopped.\n\nPlease start the service first, then try uninstall again."
                )
                return

            # After backup, ask again if they want to continue with uninstall
            button_index = self.show_native_alert(
                title="Confirm Uninstall",
                message="Proceed with uninstall?\n\nThis will permanently delete all data.",
                buttons=["Cancel", "Proceed with Uninstall"],
                default_button=0,
                cancel_button=0,
                style="warning"
            )

            if button_index != 1:  # User didn't click "Proceed"
                return

        # Step 2: Final confirmation with explicit acknowledgment
        # Use rumps.Window for text input (no osascript, no permissions needed)
        window = rumps.Window(
            message="FINAL CONFIRMATION\n\nType 'DELETE' below to confirm permanent deletion of all data:",
            title="Confirm Uninstall",
            default_text="",
            ok="Confirm Deletion",
            cancel="Cancel",
            dimensions=(320, 24)
        )

        response = window.run()
        self.log(f"Final confirmation: button={response.clicked}, text='{response.text}'")

        # Check if user clicked OK and typed "DELETE" (case insensitive)
        if response.clicked != 1:  # User clicked Cancel
            self.log("Uninstall cancelled - user clicked Cancel")
            return

        user_input = response.text.strip().upper() if response.text else ""
        if user_input != "DELETE":
            self.log(f"Uninstall cancelled - user input was: '{response.text.strip()}' (expected 'DELETE')")
            rumps.alert(
                title="Uninstall Cancelled",
                message=f"Uninstall cancelled. Type 'DELETE' to confirm.\n\n(You typed: '{response.text.strip()}')"
            )
            return

        # User confirmed uninstall - run in background thread to avoid beach ball
        def do_uninstall():
            try:
                # First, stop any ongoing setup processes
                self.log("Uninstall: Stopping any ongoing processes...")
                # Stop any ongoing browser monitoring
                self.monitoring_tor_install = False
                self.dismiss_setup_dialog()

                # Unregister from OnionHeaven before stopping (needs running containers)
                if self.is_running:
                    self.log("Uninstall: Unregistering from OnionHeaven...")
                    try:
                        onionheaven.unregister_from_onionheaven(self)
                    except Exception as e:
                        self.log(f"Uninstall: OnionHeaven unregister failed (continuing): {e}")

                # Stop the service (this will cancel any startup in progress)
                self.log("Uninstall: Stopping services...")
                subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
                self.stop_web_log_capture()
                self.stop_onion_proxy()
                self.stop_caffeinate()

                # Stop and delete Colima VM
                # Only affects OnionPress instance, not system Colima
                self.log("Uninstall: Stopping Colima VM...")
                colima_bin = os.path.join(self.bin_dir, "colima")
                env = os.environ.copy()
                env["COLIMA_HOME"] = self.colima_home
                env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
                env["LIMA_INSTANCE"] = "onionpress"
                subprocess.run([colima_bin, "stop", "-f"], capture_output=True, timeout=60, env=env)
                self.log("Uninstall: Deleting Colima VM...")
                subprocess.run([colima_bin, "delete", "-f"], capture_output=True, timeout=60, env=env)
                # Kill any orphaned colima/lima processes as a fallback
                subprocess.run(["pkill", "-f", f"{self.colima_home}"], capture_output=True, timeout=10)
                # Note: Docker volumes lived inside the Colima VM and are deleted with it

                # Remove login item LaunchAgent
                self.remove_login_item()

                # Step 3: Remove data directory (but keep it until after we show dialog)
                self.log("Uninstall: Preparing to remove data directory...")
                import shutil
                data_dir_exists = os.path.exists(self.app_support)

                # Step 4: Remove data directory
                if data_dir_exists:
                    shutil.rmtree(self.app_support)
                    self.log("Uninstall: Data directory removed successfully")

                # Step 5: Show final dialog and quit
                # Use show_native_alert which already handles main thread
                self.show_native_alert(
                    title="Uninstall Complete",
                    message="OnionPress has been uninstalled.\n\nFinal step: Move OnionPress.app to the Trash.\n\nClick OK to quit.",
                    buttons=["OK"]
                )
                rumps.quit_application()

            except Exception as e:
                # Show error and quit
                self.show_native_alert(
                    title="Uninstall Error",
                    message=f"An error occurred during uninstall:\n\n{str(e)}\n\nYou may need to manually remove:\n• ~/.onionpress directory\n• Docker volumes (if they exist)",
                    buttons=["OK"]
                )
                rumps.quit_application()

        # Run uninstall in background thread to avoid blocking UI
        threading.Thread(target=do_uninstall, daemon=True).start()

    # ── Settings Page Support ─────────────────────────────────────

    def write_status_to_volume(self):
        """Write status.json, config-current.json, recent-logs.txt to the shared Docker volume
        so the WordPress settings/status pages can display current state."""
        try:
            # Determine state
            if not self.is_running:
                state = "stopped"
            elif self.is_ready:
                state = "running"
            elif not self._has_internet:
                state = "offline"
            elif self._yellow_since and (time.time() - self._yellow_since) > 300:
                state = "stuck"
            else:
                state = "starting"

            # Get container states
            containers = {}
            try:
                result = subprocess.run(
                    ["docker", "ps", "--format", "{{.Names}}\t{{.State}}",
                     "--filter", "name=onionpress"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=5
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        parts = line.split("\t", 1)
                        if len(parts) == 2:
                            containers[parts[0]] = parts[1]
            except Exception:
                pass

            # Get uptime
            uptime_seconds = int(time.time() - self.startup_time) if self.is_running else 0

            # Bootstrap percentage
            bootstrap_pct = self._last_bootstrap_pct if hasattr(self, '_last_bootstrap_pct') else 0
            if self.is_ready:
                bootstrap_pct = 100

            # Wayback queue count
            wq_count = 0
            try:
                with self._wayback_queue_lock:
                    wq_count = len(self._wayback_queue)
            except Exception:
                pass

            # OnionHeaven stats
            oh_server_active = getattr(self, 'is_onionheaven', False)
            oh_stats = {'server_active': oh_server_active, 'client_registered': False,
                        'client_enabled': True, 'client_hub': '', 'registered_count': 0,
                        'online_count': 0, 'taken_over_count': 0, 'takeover_containers': 0}
            if oh_server_active:
                try:
                    result = subprocess.run(
                        ["docker", "exec", "onionheaven", "sqlite3",
                         "/var/lib/onionpress/onionheaven/registry.db",
                         "SELECT COUNT(*), SUM(CASE WHEN status='online' THEN 1 ELSE 0 END), "
                         "SUM(CASE WHEN status='taken-over' THEN 1 ELSE 0 END) FROM registry"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        parts = result.stdout.strip().split("|")
                        oh_stats['registered_count'] = int(parts[0] or 0)
                        oh_stats['online_count'] = int(parts[1] or 0)
                        oh_stats['taken_over_count'] = int(parts[2] or 0)
                except Exception:
                    pass
                try:
                    result = subprocess.run(
                        ["docker", "ps", "--format", "{{.Names}}",
                         "--filter", "name=onionheaven-takeover"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    if result.returncode == 0:
                        oh_stats['takeover_containers'] = len([
                            l for l in result.stdout.strip().splitlines() if l.strip()
                        ])
                except Exception:
                    pass
            oh_stats['client_enabled'] = self._read_config_value("REGISTER_WITH_ONIONHEAVEN", "yes") == "yes"
            oh_stats['client_hub'] = self._read_config_value(
                "ONIONHEAVEN_ADDRESS", "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion")

            onion_addr = self.onion_address if self.onion_address and ".onion" in str(self.onion_address) else ""

            # System load averages and host uptime
            load_avg = list(os.getloadavg())
            try:
                host_uptime = int(time.time() - self._host_boot_time)
            except AttributeError:
                # Cache boot time on first call
                try:
                    r = subprocess.run(["sysctl", "-n", "kern.boottime"],
                                       capture_output=True, text=True, timeout=2)
                    # Output like: { sec = 1710345600, usec = 0 } ...
                    import re
                    m = re.search(r'sec\s*=\s*(\d+)', r.stdout)
                    self._host_boot_time = int(m.group(1)) if m else time.time()
                except Exception:
                    self._host_boot_time = time.time()
                host_uptime = int(time.time() - self._host_boot_time)

            import datetime
            status = {
                'state': state,
                'version': self.version,
                'onion_address': onion_addr,
                'tor_impl': self._read_config_value("TOR_IMPL", "arti"),
                'uptime_seconds': uptime_seconds,
                'bootstrap_pct': bootstrap_pct,
                'containers': containers,
                'wayback_queue_count': wq_count,
                'updated_at': datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                'platform': 'macos',
                'load_avg': load_avg,
                'host_uptime_seconds': host_uptime,
                'onionheaven': oh_stats,
            }

            status_json = json.dumps(status, indent=2)

            # Write status.json
            subprocess.run(
                ["docker", "exec", "-i", "onionpress-wordpress",
                 "tee", "/var/lib/onionpress/status.json"],
                input=status_json, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=5
            )

            # Write version file
            subprocess.run(
                ["docker", "exec", "-i", "onionpress-wordpress",
                 "tee", "/var/lib/onionpress/version"],
                input=self.version, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=5
            )

            # Write config-current.json
            config = {}
            config_file = os.path.join(self.app_support, "config")
            try:
                with open(config_file, encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if '=' in line:
                            key, val = line.split('=', 1)
                            config[key] = val
            except (OSError, IOError):
                pass

            config_json = json.dumps(config, indent=2)
            subprocess.run(
                ["docker", "exec", "-i", "onionpress-wordpress",
                 "tee", "/var/lib/onionpress/config-current.json"],
                input=config_json, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=5
            )

            # Write recent logs
            log_file = os.path.join(self.app_support, "onionpress.log")
            try:
                with open(log_file, encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                recent = "".join(lines[-100:])
                subprocess.run(
                    ["docker", "exec", "-i", "onionpress-wordpress",
                     "tee", "/var/lib/onionpress/recent-logs.txt"],
                    input=recent, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=5
                )
            except (OSError, IOError):
                pass

        except Exception:
            pass  # Container may not be running

    def poll_config_updates(self):
        """Check for config changes written by the WordPress settings page."""
        try:
            result = subprocess.run(
                ["docker", "exec", "onionpress-wordpress",
                 "cat", "/var/lib/onionpress/config-updates.json"],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=5
            )
            if result.returncode != 0 or not result.stdout.strip():
                return

            updates = json.loads(result.stdout.strip())
            if not updates or not isinstance(updates, dict):
                return

            self.log(f"Settings page: applying {len(updates)} config update(s)")

            # Apply each setting to ~/.onionpress/config
            for key, val in updates.items():
                old_val = self._read_config_value(key)
                if old_val != val:
                    self.write_config_value(key, val)
                    self.log(f"  {key}: {old_val!r} → {val!r}")

            # Delete the updates file
            subprocess.run(
                ["docker", "exec", "onionpress-wordpress",
                 "rm", "-f", "/var/lib/onionpress/config-updates.json"],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=5
            )

            # Re-read settings that the menubar app uses at runtime
            self.cloudflare_tunnel_enabled = bool(self._read_config_value("CLOUDFLARE_TUNNEL_TOKEN"))

        except json.JSONDecodeError:
            pass
        except Exception:
            pass  # Container may not be running

    def poll_requested_actions(self):
        """Check for action requests from the WordPress settings page."""
        try:
            result = subprocess.run(
                ["docker", "exec", "onionpress-wordpress",
                 "cat", "/var/lib/onionpress/requested-action"],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=5
            )
            if result.returncode != 0 or not result.stdout.strip():
                return

            action = result.stdout.strip()

            # Clear the action file immediately
            subprocess.run(
                ["docker", "exec", "onionpress-wordpress",
                 "sh", "-c", "rm -f /var/lib/onionpress/requested-action /var/lib/onionpress/service-result.json"],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=5
            )

            if action == "refresh-status":
                self.write_status_to_volume()
                return

            self.log(f"Settings page: handling action '{action}'")

            # Run action in background thread so it doesn't block the polling loop
            threading.Thread(
                target=self._handle_requested_action, args=(action,),
                daemon=True
            ).start()

        except Exception:
            pass

    def _handle_requested_action(self, action):
        """Execute a requested action from the WordPress settings page."""
        import datetime

        def _write_result(filename, data):
            """Write a JSON result file to the WordPress shared volume."""
            try:
                result_json = json.dumps(data)
                subprocess.run(
                    ["docker", "exec", "-i", "onionpress-wordpress",
                     "tee", f"/var/lib/onionpress/{filename}"],
                    input=result_json, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=5
                )
            except Exception:
                pass

        def _clear_pending():
            try:
                subprocess.run(
                    ["docker", "exec", "onionpress-wordpress",
                     "sh", "-c", "rm -f /var/lib/onionpress/service-pending"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=5
                )
            except Exception:
                pass

        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            if action in ("restart", "start"):
                # Write pending marker
                try:
                    subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "sh", "-c", f"echo {action} > /var/lib/onionpress/service-pending"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                except Exception:
                    pass

                if action == "restart":
                    self.log("Settings page: restarting OnionPress...")
                    self.run_command("restart")
                else:
                    self.log("Settings page: starting OnionPress...")
                    self.run_command("start")

                # Wait for services to come back
                import time
                for _ in range(30):
                    time.sleep(2)
                    if self.is_running:
                        break

                self.write_status_to_volume()
                containers_info = ""
                try:
                    r = subprocess.run(
                        ["docker", "ps", "--filter", "name=onionpress", "--format", "{{.Names}}: {{.Status}}"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5
                    )
                    if r.returncode == 0:
                        containers_info = r.stdout.strip().replace("onionpress-", "").replace("\n", ", ")
                except Exception:
                    pass
                _write_result("service-result.json", {
                    "success": True, "action": action,
                    "message": f"{'Restarted' if action == 'restart' else 'Started'} {containers_info}",
                    "completed_at": now_iso
                })
                _clear_pending()

            elif action == "stop":
                self.log("Settings page: stopping OnionPress...")
                self.run_command("stop")
                _write_result("service-result.json", {
                    "success": True, "action": "stop",
                    "message": "OnionPress stopped", "completed_at": now_iso
                })

            elif action == "check-reachability":
                self.log("Settings page: running reachability test...")
                onion_addr = self.onion_address if self.onion_address and ".onion" in str(self.onion_address) else ""
                if not onion_addr:
                    _write_result("reachability-result.json", {
                        "reachable": False, "error": "No onion address found",
                        "tested_at": now_iso
                    })
                else:
                    try:
                        r = subprocess.run(
                            ["docker", "exec", "onionpress-tor-client",
                             "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                             "--max-time", "60", "--socks5-hostname", "127.0.0.1:9050",
                             f"http://{onion_addr}/"],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=75
                        )
                        http_code = r.stdout.strip() or "000"
                        reachable = http_code.isdigit() and 200 <= int(http_code) < 500
                    except Exception:
                        http_code = "000"
                        reachable = False
                    _write_result("reachability-result.json", {
                        "reachable": reachable, "http_code": http_code,
                        "address": onion_addr, "tested_at": now_iso
                    })
                    self.log(f"Settings page: reachability test -> HTTP {http_code} (reachable={reachable})")

            elif action == "generate-vanity":
                self.log("Settings page: generating vanity address...")
                prefix = self._read_config_value("ADDRESS_PREFIX", "op2")
                try:
                    # Use the onionpress script's prefix change mechanism:
                    # stop → delete arti state volume → start (triggers key regen)
                    self.run_command("stop")
                    import time
                    time.sleep(2)
                    # Delete the arti state volume so a new key is generated on next start
                    subprocess.run(
                        ["docker", "volume", "rm", "onionpress-arti-state"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=15
                    )
                    self.run_command("start")
                    for _ in range(60):
                        time.sleep(2)
                        if self.is_running and self.onion_address and ".onion" in str(self.onion_address):
                            break
                    self.write_status_to_volume()
                    new_addr = self.onion_address or ""
                    _write_result("vanity-result.json", {
                        "success": True, "address": new_addr,
                        "generated_at": now_iso
                    })
                    self.log(f"Settings page: vanity address generated: {new_addr}")
                except Exception as e:
                    _write_result("vanity-result.json", {
                        "success": False, "error": str(e),
                        "generated_at": now_iso
                    })
                    self.log(f"Settings page: vanity generation failed: {e}")

            elif action == "import-key-file":
                self.log("Settings page: importing key...")
                try:
                    r = subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "cat", "/var/lib/onionpress/import-key-data"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    key_data = r.stdout.strip() if r.returncode == 0 else ""
                    subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "rm", "-f", "/var/lib/onionpress/import-key-data"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    if not key_data:
                        _write_result("import-result.json", {
                            "success": False, "error": "No key data found",
                            "imported_at": now_iso
                        })
                    else:
                        # Use the onionpress script's import-key command
                        r = subprocess.run(
                            [self.launcher_script, "import-key", key_data],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=120
                        )
                        if r.returncode == 0:
                            # Restart to pick up the imported key
                            self.run_command("restart")
                            import time
                            for _ in range(60):
                                time.sleep(2)
                                if self.is_running and self.onion_address and ".onion" in str(self.onion_address):
                                    break
                            self.write_status_to_volume()
                            new_addr = self.onion_address or ""
                            _write_result("import-result.json", {
                                "success": True, "address": new_addr,
                                "imported_at": now_iso
                            })
                            self.log(f"Settings page: key imported, address: {new_addr}")
                        else:
                            error_msg = r.stderr.strip() or r.stdout.strip() or "Import failed"
                            _write_result("import-result.json", {
                                "success": False, "error": error_msg,
                                "imported_at": now_iso
                            })
                            self.log(f"Settings page: key import failed: {error_msg}")
                except Exception as e:
                    _write_result("import-result.json", {
                        "success": False, "error": str(e),
                        "imported_at": now_iso
                    })
                    self.log(f"Settings page: key import failed: {e}")

            elif action == "create-backup":
                self.log("Settings page: creating backup...")
                try:
                    r = subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "cat", "/var/lib/onionpress/backup-password"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    password = r.stdout.strip() if r.returncode == 0 else ""
                    subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "rm", "-f", "/var/lib/onionpress/backup-password"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    if not password:
                        _write_result("backup-result.json", {
                            "success": False, "error": "No password provided",
                            "created_at": now_iso
                        })
                    else:
                        import tempfile
                        onion_short = (self.onion_address or "site").replace(".onion", "")[:8]
                        import time
                        filename = f"OnionPress-{onion_short}-{time.strftime('%Y-%m-%d-%H-%M')}.zip"
                        tmp_path = os.path.join(tempfile.gettempdir(), filename)

                        import backup_manager
                        onion_addr = self.onion_address or "unknown"
                        backup_manager.create_backup(
                            onion_addr, "admin", password,
                            tmp_path, self.version, self.log
                        )

                        # Copy into WordPress container for download
                        subprocess.run(
                            ["docker", "cp", tmp_path,
                             f"onionpress-wordpress:/var/lib/onionpress/{filename}"],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=30
                        )
                        subprocess.run(
                            ["docker", "exec", "onionpress-wordpress",
                             "chown", "www-data:www-data", f"/var/lib/onionpress/{filename}"],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=5
                        )
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
                        _write_result("backup-result.json", {
                            "success": True, "filename": filename,
                            "created_at": now_iso
                        })
                        self.log(f"Settings page: backup created: {filename}")
                except Exception as e:
                    _write_result("backup-result.json", {
                        "success": False, "error": str(e),
                        "created_at": now_iso
                    })
                    self.log(f"Settings page: backup failed: {e}")

            elif action == "restore-backup":
                self.log("Settings page: restoring from backup...")
                try:
                    r = subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "cat", "/var/lib/onionpress/restore-password"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    password = r.stdout.strip() if r.returncode == 0 else ""
                    subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "rm", "-f", "/var/lib/onionpress/restore-password"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )

                    import tempfile
                    local_zip = os.path.join(tempfile.gettempdir(), "onionpress-restore.zip")
                    subprocess.run(
                        ["docker", "cp",
                         "onionpress-wordpress:/var/lib/onionpress/restore-upload.zip",
                         local_zip],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=60
                    )
                    subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "rm", "-f", "/var/lib/onionpress/restore-upload.zip"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )

                    if not password or not os.path.exists(local_zip):
                        _write_result("restore-result.json", {
                            "success": False, "error": "Missing password or backup file",
                            "restored_at": now_iso
                        })
                    else:
                        import backup_manager
                        backup_manager.restore_from_backup(local_zip, password, self.log)
                        try:
                            os.remove(local_zip)
                        except OSError:
                            pass
                        # Restart to pick up restored keys
                        self.run_command("restart")
                        import time
                        for _ in range(30):
                            time.sleep(2)
                            if self.is_running:
                                break
                        self.write_status_to_volume()
                        new_addr = self.onion_address or ""
                        _write_result("restore-result.json", {
                            "success": True, "address": new_addr,
                            "restored_at": now_iso
                        })
                        self.log(f"Settings page: restore complete, address: {new_addr}")
                except Exception as e:
                    _write_result("restore-result.json", {
                        "success": False, "error": str(e),
                        "restored_at": now_iso
                    })
                    self.log(f"Settings page: restore failed: {e}")

            else:
                self.log(f"Settings page: unknown action '{action}'")

        except Exception as e:
            self.log(f"Settings page: action '{action}' failed: {e}")

    # ── Wayback Queue ──────────────────────────────────────────────

    def poll_wayback_queue(self):
        """Read the Wayback queue from the WordPress container's shared volume."""
        try:
            result = subprocess.run(
                ["docker", "exec", "onionpress-wordpress",
                 "cat", "/var/lib/onionpress/wayback-queue.json"],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                queue = json.loads(result.stdout.strip())
                with self._wayback_queue_lock:
                    self._wayback_queue = queue if isinstance(queue, list) else []
            else:
                with self._wayback_queue_lock:
                    self._wayback_queue = []
        except Exception:
            pass  # Container may not be running

    def drain_wayback_queue(self):
        """Process one item from the Wayback queue (rate-limited to 1 per 30s)."""
        now = time.time()
        if now - self._wayback_last_drain < 30:
            return

        with self._wayback_queue_lock:
            if not self._wayback_queue:
                return
            item = self._wayback_queue[0]

        url = item.get("url", "")
        if not url:
            self._remove_wayback_queue_item(url)
            return

        self._wayback_last_drain = now
        self.log(f"Wayback queue: archiving {url}")

        try:
            # Try clearnet endpoint first (faster)
            result = subprocess.run(
                ["docker", "exec", "onionpress-wordpress",
                 "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "30",
                 "--data-urlencode", f"url={url}",
                 "https://web.archive.org/save"],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=45
            )
            http_code = result.stdout.strip()

            if http_code and 200 <= int(http_code) < 500:
                self.log(f"Wayback queue: archived {url} (HTTP {http_code})")
                self._remove_wayback_queue_item(url)
                return
        except Exception as e:
            self.log(f"Wayback queue: clearnet failed for {url}: {e}")

        try:
            # Fall back to .onion endpoint via tor container
            result = subprocess.run(
                ["docker", "exec", "onionpress-tor",
                 "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "30",
                 "--socks5-hostname", "127.0.0.1:9050",
                 "--data-urlencode", f"url={url}",
                 "http://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save"],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=45
            )
            http_code = result.stdout.strip()

            if http_code and 200 <= int(http_code) < 500:
                self.log(f"Wayback queue: archived {url} via Tor (HTTP {http_code})")
                self._remove_wayback_queue_item(url)
            else:
                self.log(f"Wayback queue: failed for {url} (HTTP {http_code})")
        except Exception as e:
            self.log(f"Wayback queue: tor endpoint failed for {url}: {e}")

    def _remove_wayback_queue_item(self, url):
        """Remove a URL from the Wayback queue in the container."""
        with self._wayback_queue_lock:
            self._wayback_queue = [i for i in self._wayback_queue if i.get("url") != url]
            updated = json.dumps(self._wayback_queue)
        try:
            subprocess.run(
                ["docker", "exec", "-i", "onionpress-wordpress",
                 "tee", "/var/lib/onionpress/wayback-queue.json"],
                input=updated, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=5
            )
        except Exception:
            pass

    @rumps.clicked("Quit")
    def quit_app(self, _):
        """Quit the application"""
        self.log("="*60)
        self.log("QUIT BUTTON CLICKED - v2.4.32 RUNNING")
        self.log("="*60)
        self._quitting = True  # Prevent _handle_terminate from running again

        # Stop monitoring immediately
        self.monitoring_tor_install = False
        self.dismiss_setup_dialog()
        self.stop_web_log_capture()

        # Close any open log viewer windows
        _LogViewerWindow.close_all()

        # Show stopped icon and status during shutdown — stays visible until
        # all services are actually stopped (prevents port conflicts on relaunch)
        def show_stopping():
            self.menu["Starting..."].title = "Quitting..."
            self.icon = self.icon_stopped
        _main_thread(show_stopping)

        def cleanup_and_quit():
            # Small delay to ensure UI updates
            time.sleep(0.5)

            # Stop onion proxy first (release port 9077 immediately)
            self.stop_onion_proxy()

            # Stop caffeinate to allow Mac to sleep
            self.stop_caffeinate()

            # Notify OnionHeaven before stopping services (containers needed for curl)
            if self._onionheaven_registration_succeeded and not self.is_onionheaven:
                try:
                    onionheaven.notify_onionheaven_offline(self)
                except Exception:
                    pass

            # Now run cleanup — 90s timeout for OnionHeaven farm containers
            try:
                self.log("Stopping services...")
                subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=90)
                self.log("Services stopped")
            except subprocess.TimeoutExpired:
                self.log("Warning: Stop command timed out")
            except Exception as e:
                self.log(f"Warning: Stop failed: {e}")

            try:
                colima_bin = os.path.join(self.bin_dir, "colima")
                self.log("Stopping Colima VM...")
                env = os.environ.copy()
                env["COLIMA_HOME"] = self.colima_home
                env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
                env["LIMA_INSTANCE"] = "onionpress"
                subprocess.run([colima_bin, "stop"], capture_output=True, timeout=60, env=env)
                self.log("Colima stopped")
            except subprocess.TimeoutExpired:
                self.log("Warning: Colima stop timed out")
            except Exception as e:
                self.log(f"Warning: Colima stop failed: {e}")

            # Remove PID file
            self._remove_pid_file()

            self.log("Cleanup complete, exiting")

            # Now quit (must dispatch to main thread)
            _main_thread(rumps.quit_application)

        # Non-daemon thread so the app stays alive until cleanup finishes
        threading.Thread(target=cleanup_and_quit, daemon=False).start()

if __name__ == "__main__":
    OnionPressApp().run()
