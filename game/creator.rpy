## The card creator, started from inside the game.
##
## The creator is a small web server (creator/server.py) plus a page in the browser. A player who
## downloaded the game has no Python of their own to run it with, but the game carries one, so the
## game runs it: in the background, on this computer only, working on the same cards and presets
## the game plays. It stops when the game is closed.
##
## Started with SI_STATION_CREATOR=1 in the environment (what the "SI-Station Creator" launchers
## do), the game skips its own start-up and only shows the creator.

init python:
    import os

    class CreatorStatus(object):
        url = ""
        error = ""

    creator = CreatorStatus()

    def creator_available():
        return not (renpy.android or renpy.ios or renpy.emscripten) and os.path.isfile(os.path.join(config.basedir, "creator", "server.py"))

    def start_creator():
        """Starts the creator's server if it is not running yet. Raises if it cannot."""
        if creator.url:
            return
        import sys
        import threading
        from http.server import ThreadingHTTPServer
        folder = os.path.join(config.basedir, "creator")
        if folder not in sys.path:
            sys.path.insert(0, folder)
        import server as creator_server
        creator_server.Handler.workspace = creator_server.Workspace(cards_dir(), os.path.join(data_dir(), "exports"), preset_folder())
        httpd = None
        for port in range(8770, 8790):          # the usual one, or the next free one if a creator is already open
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), creator_server.Handler)
                break
            except OSError:
                continue
        if httpd is None:
            raise OSError("no free port between 8770 and 8789")
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        creator.url = "http://127.0.0.1:%d" % httpd.server_address[1]

    def open_creator():
        """Starts the creator if need be and opens it in the browser."""
        import webbrowser
        creator.error = ""
        try:
            start_creator()
            webbrowser.open(creator.url)
            renpy.notify("The card creator is open in your browser.")
        except Exception as e:
            creator.error = "The card creator could not be started: %s" % e
            renpy.notify(creator.error)
        renpy.restart_interaction()

    def creator_only():
        return bool(os.environ.get("SI_STATION_CREATOR")) and creator_available()


## What the window shows when the game was started only to run the creator.
screen creator_window():
    add "#0f141a"
    vbox:
        align (0.5, 0.45)
        spacing 26
        text "SI-Station" size 90 color "#ffffff" xalign 0.5
        text _("Card Creator") size 44 color "#5aa9f5" xalign 0.5
        null height 10
        if creator.error:
            text esc(creator.error) color "#ff8080" xalign 0.5
        else:
            text _("The creator is open in your browser. Keep this window open while you use it.") size 28 xalign 0.5
            text esc(creator.url) size 26 color "#999999" xalign 0.5
        hbox:
            xalign 0.5
            spacing 60
            textbutton _("Open it again") action Function(open_creator)
            textbutton _("Play the game") action Return("play")
            textbutton _("Quit") action Quit(confirm=False)
