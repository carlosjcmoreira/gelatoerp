import os

bind = "0.0.0.0:5000"
workers = 4
worker_class = "sync"
timeout = 120
keepalive = 5
reuse_port = True

loglevel = "info"
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'


def post_fork(server, worker):
    """Start the Google Sheets background scheduler in the first worker only.

    worker.age == 1 identifies the first worker spawned at startup.
    If that worker restarts, the scheduler will not migrate to a new worker.
    This is acceptable: a missed daily sync retries the next day.
    For higher availability, replace with a process-level lock (e.g. fcntl).
    """
    if worker.age == 1 and os.environ.get('EVENTOS_SYNC_ENABLED', '1') == '1':
        from flask_app.app import _start_sheets_sync_scheduler
        server.log.info("Starting Google Sheets sync scheduler in worker %s (age=%s)", worker.pid, worker.age)
        _start_sheets_sync_scheduler()
