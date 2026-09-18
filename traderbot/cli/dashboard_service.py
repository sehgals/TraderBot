"""Windowless scheduled-task entry point with bounded service logs."""
import logging
import logging.config
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[2]
    logs = root / 'runtime/logs'
    logs.mkdir(parents=True, exist_ok=True)
    config = {
        'version': 1, 'disable_existing_loggers': False,
        'formatters': {'service': {'format': '%(asctime)s %(levelname)s %(name)s %(message)s'}},
        'handlers': {'service': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(logs / 'dashboard-service.log'),
            'maxBytes': 5 * 1024 * 1024, 'backupCount': 3,
            'encoding': 'utf-8', 'formatter': 'service',
        }},
        'root': {'handlers': ['service'], 'level': 'INFO'},
        'loggers': {name: {'handlers': [], 'propagate': True, 'level': 'INFO'}
                    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access')},
    }
    logging.config.dictConfig(config)
    try:
        from traderbot.cli.dashboard import main as run
        run(log_config=config)
    except BaseException:
        logging.getLogger(__name__).exception('Dashboard service stopped with an error')
        raise


if __name__ == '__main__':
    main()
