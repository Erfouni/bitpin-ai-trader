"""Bitpin trading toolkit: data, backtesting, strategies, paper and live execution.

__version__ is the release the server runs (CHANGELOG.md): the startup banner prints it and
`bitpin-bot status` / the notifier can show it, so a Telegram screenshot or a journal line tells at once
which code made a decision. It is a plain string on purpose (no packaging metadata, stdlib only, importable
on Python 3.7): deploy/update.sh compares trees by content, never by this value, so a forgotten bump can
never block an update - it only mislabels the banner. Semantic-ish: MAJOR = a release the owner must
confirm-live again for (what the bot does on its own changed), MINOR = settings / notifier / docs,
PATCH = fixes."""

__version__ = "3.8.2"
