"""qlstats (PredatH0r/XonStat) adapter.

Verified against the live service 2026-09-22 (see monorepo
docs/superpowers/specs/2026-09-22-qlsm-player-rating-sources-design.md,
section 9.7) -- not just the API docs. Two things the original integration
plan got wrong from reading balance.py alone, both load-bearing here:

- Rated game types are exactly duel/ffa/ca/tdm/ctf/ft/ad (7). Not
  har/dom/1f/rr/race -- those simply are not queried.
- qlstats never returns a bucket with games=0 for a *tracked* player; that
  shape is the server's own default for a player it has never seen
  (`{"elo": 900, "games": 0}`), returned for every unknown id on every
  gametype. Treating games<=0 as "no data" is the only way to avoid showing
  a fabricated 900 for players qlstats has simply never heard of.
"""
import re

import requests

from .base import PROVIDER_TIMEOUT_SEC, RankProvider

DEFAULT_BASE_URL = 'http://qlstats.net'
DEFAULT_RATING_SYSTEM = 'elo'
# Operator-typed API path, e.g. 'elo', 'elo_b', 'elo/bn' (mirrors qlx_balanceApi).
_RATING_PATH_RE = re.compile(r'^[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*$')
RATED_GAMETYPES = {'duel', 'ffa', 'ca', 'tdm', 'ctf', 'ft', 'ad'}


class QlstatsProvider(RankProvider):
    def __init__(self, base_url=None, api_key=None, extra=None):
        super().__init__(base_url, api_key, extra)
        if not self.base_url:
            self.base_url = DEFAULT_BASE_URL
        rating_system = (self.extra.get('rating_system') or '').strip().strip('/')
        self.rating_system = rating_system if _RATING_PATH_RE.match(rating_system) else DEFAULT_RATING_SYSTEM

    def map_game_type(self, qlsm_gametype):
        gt = (qlsm_gametype or '').strip().lower()
        return gt if gt in RATED_GAMETYPES else None

    def fetch_ratings(self, steam_ids, game_type):
        if not steam_ids or not game_type:
            return {}
        ids_path = '+'.join(steam_ids)
        # No try/except: a failed call must escape so ranks_service logs it
        # and caches the empty answer for TTL_NEGATIVE, not TTL_SUCCESS.
        resp = requests.get(
            f'{self.base_url}/{self.rating_system}/{ids_path}',
            timeout=PROVIDER_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return {}

        wanted = set(steam_ids)
        out = {}
        for player in data.get('players') or []:
            if not isinstance(player, dict):
                continue
            steam_id = str(player.get('steamid') or player.get('steam_id') or '')
            if steam_id not in wanted:
                continue
            bucket = player.get(game_type)
            if not isinstance(bucket, dict):
                continue
            games = bucket.get('games') or 0
            elo = bucket.get('elo')
            if games <= 0 or elo is None:
                continue
            out[steam_id] = {
                'rating': float(elo),
                'display': str(int(elo)),
                'provisional': False,
                'title': f'{game_type}, {games} games',
            }
        return out
