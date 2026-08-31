# Authenticated page lookup benchmark

Measured on 2026-08-31 from the `request_metrics` log emitted by the
authenticated home route (`GET /`) against the same development database.

Method:

- four Gunicorn sync workers, matching `gunicorn.conf.py`;
- one login request followed by 30 sequential authenticated home requests;
- warm-up samples from each worker excluded from the steady-state comparison;
- p95 calculated with the nearest-rank method over 27 steady-state samples.

| Version | SQL queries per request | Request duration p95 |
| --- | ---: | ---: |
| Base revision | 32 | 27.3 ms |
| Optimized | 28 | 28.4 ms |

The steady-state query count fell by 12.5%. The task snapshot reported about
39 queries for its original authenticated home sample; differences depend on
the user's enabled dashboard modules and worker cache warmth. Local request
p95 remained effectively flat, while production avoids four database network
round trips per page.