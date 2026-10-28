# Dependency notices

The repository's license remains in `LICENSE`. Dependencies retain their own licenses.
The image preserves distro notices in `/usr/share/doc` and installed Python distributions'
license metadata in `/app/.venv/lib/python3.12/site-packages`.

- Stockfish 15.1 (Debian package 15.1-4): GPL-3.0; copyright/license at
  `/usr/share/doc/stockfish/copyright`. Source: <https://stockfishchess.org>,
  <https://sources.debian.org/src/stockfish/15.1-4/>.
- `chess` 1.11.2 (python-chess): GPL-3.0-or-later. Source/license:
  <https://github.com/niklasf/python-chess>.
- LangGraph: MIT; OpenAI Python SDK: Apache-2.0; Pydantic: MIT;
  HTTPX: BSD-3-Clause; python-telegram-bot: LGPL-3.0.

Before distributing an image, review redistribution obligations and provide the corresponding
source/license material required by GPL/LGPL dependencies. This local build is not a distribution
or a claim that the repository license replaces those terms.
