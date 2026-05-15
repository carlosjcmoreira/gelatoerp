import os

bind = "0.0.0.0:5000"
workers = 4
worker_class = "sync"
timeout = 300
keepalive = 5
reuse_port = True

loglevel = "info"
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'


def post_fork(server, worker):
    """Start background schedulers in the first worker only.

    worker.age == 1 identifies the first worker spawned at startup.
    Running schedulers in a single worker avoids duplicate fetches and
    race conditions when multiple workers try to upsert the same data.
    """
    if worker.age == 1:
        if os.environ.get('EVENTOS_SYNC_ENABLED', '1') == '1':
            from flask_app.app import _start_sheets_sync_scheduler
            server.log.info("Starting Google Sheets sync scheduler in worker %s (age=%s)", worker.pid, worker.age)
            _start_sheets_sync_scheduler()

        import weather_scheduler as wsch
        server.log.info("Starting weather scheduler in worker %s (age=%s)", worker.pid, worker.age)
        wsch.start_weather_scheduler()
