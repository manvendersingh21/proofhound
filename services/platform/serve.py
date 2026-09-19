"""Launch the ProofHound web app on loopback: python -m services.platform.serve [--port 8080]

Sets up one local data directory, points the runner's OTLP exporter at the app's own
receiver (/otlp/v1/traces) so browser steps and instrumented backends land in the same
trace index, and allows log tailing under that data directory.
"""
import argparse
import os
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description='ProofHound local web app')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--data', default='.local-runs/app')
    parser.add_argument('--no-local-files', action='store_true', help='disallow server log tailing')
    args = parser.parse_args(argv)
    data = Path(args.data).resolve()
    data.mkdir(parents=True, exist_ok=True)
    os.environ['QA_DATA_DIR'] = str(data)
    os.environ.setdefault('QA_OTLP_TRACES_ENDPOINT', f'http://127.0.0.1:{args.port}/otlp/v1/traces')
    if not args.no_local_files:
        os.environ['QA_API_LOCAL_FILES'] = '1'
        os.environ.setdefault('QA_LOG_ROOT', str(data))
    import uvicorn
    print(f'ProofHound: http://127.0.0.1:{args.port}/   data: {data}')
    uvicorn.run('services.platform.api:app', host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
