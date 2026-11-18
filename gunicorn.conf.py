import multiprocessing, os
bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
workers = int(os.getenv("WEB_CONCURRENCY", min(multiprocessing.cpu_count() * 2 + 1, 4)))
threads = 2
timeout = 30
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = "*"  # only safe behind your own reverse proxy / platform router
