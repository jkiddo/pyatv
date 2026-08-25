# Apple TV web remote

A small local web remote built directly on top of [`pyatv`](https://pyatv.dev/).

## Set up

Python 3.9 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
atvremote wizard
```

The wizard pairs the computer with the Apple TV and saves the credentials in
the standard pyatv settings file. It normally only needs to be run once.

## Run

```bash
source .venv/bin/activate
python tutorial.py
```

Open <http://127.0.0.1:8080>. To use it from a phone on the same network, open
`http://YOUR-COMPUTER-IP:8080` instead.

The web server is intentionally local and has no authentication. Do not expose
port 8080 to the public internet.
