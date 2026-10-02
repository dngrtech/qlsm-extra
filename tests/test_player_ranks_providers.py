"""Unit tests for the rating-source adapters, importable standalone (no
Flask app, no qlsm checkout needed for these specific classes -- they only
touch `requests` and the abc-based RankProvider contract).
"""
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

ADDON_DIR = Path(__file__).resolve().parent.parent / 'addons' / 'player-ranks'
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))

from providers.base import RateLimited  # noqa: E402
from providers.elo_service import ThunderdomeEloProvider  # noqa: E402
from providers.qlstats import QlstatsProvider  # noqa: E402
from providers.slipgate import SlipgateProvider  # noqa: E402


def _resp(status_code=200, json_data=None, headers=None):
    resp = Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_data if json_data is not None else {}
    if status_code >= 400:
        import requests
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.side_effect = None
    return resp


# ---- qlstats -------------------------------------------------------------

class TestQlstats:
    def test_rated_gametypes_map_to_themselves(self):
        p = QlstatsProvider()
        for gt in ('duel', 'ffa', 'ca', 'tdm', 'ctf', 'ft', 'ad'):
            assert p.map_game_type(gt) == gt

    def test_unrated_gametype_is_none(self):
        p = QlstatsProvider()
        assert p.map_game_type('har') is None
        assert p.map_game_type('race') is None

    def test_games_zero_is_treated_as_no_data_not_elo_900(self):
        """The qlstats default-for-unknown-player shape, not a real rating."""
        p = QlstatsProvider(base_url='http://qlstats.example')
        payload = {'players': [
            {'steamid': '76561197993968023', 'duel': {'games': 13732, 'elo': 2181}},
            {'steamid': '76561197960287930', 'duel': {'games': 0, 'elo': 900}},
        ]}
        with patch('providers.qlstats.requests.get', return_value=_resp(json_data=payload)):
            result = p.fetch_ratings(['76561197993968023', '76561197960287930'], 'duel')

        assert result['76561197993968023']['display'] == '2181'
        assert '76561197960287930' not in result

    def test_empty_steam_ids_short_circuits(self):
        p = QlstatsProvider()
        assert p.fetch_ratings([], 'duel') == {}

    def test_no_game_type_short_circuits(self):
        p = QlstatsProvider()
        assert p.fetch_ratings(['76561197993968023'], None) == {}

    def test_network_failure_raises(self):
        import requests
        p = QlstatsProvider()
        with patch('providers.qlstats.requests.get', side_effect=requests.ConnectionError()):
            with pytest.raises(requests.ConnectionError):
                p.fetch_ratings(['76561197993968023'], 'duel')

    def test_http_error_raises(self):
        import requests
        p = QlstatsProvider()
        with patch('providers.qlstats.requests.get', return_value=_resp(status_code=500)):
            with pytest.raises(requests.HTTPError):
                p.fetch_ratings(['76561197993968023'], 'duel')

    def test_rating_system_defaults_to_elo_and_is_used_in_url(self):
        p = QlstatsProvider(base_url='http://qlstats.example')
        with patch('providers.qlstats.requests.get', return_value=_resp(json_data={'players': []})) as mock_get:
            p.fetch_ratings(['76561197993968023'], 'duel')
        assert mock_get.call_args[0][0] == 'http://qlstats.example/elo/76561197993968023'

    def test_rating_system_override(self):
        p = QlstatsProvider(base_url='http://qlstats.example', extra={'rating_system': 'elo_b'})
        with patch('providers.qlstats.requests.get', return_value=_resp(json_data={'players': []})) as mock_get:
            p.fetch_ratings(['76561197993968023'], 'duel')
        assert '/elo_b/' in mock_get.call_args[0][0]

    def test_rating_path_may_have_a_subpath(self):
        p = QlstatsProvider(base_url='http://qlstats.example', extra={'rating_system': ' /elo/bn/ '})
        with patch('providers.qlstats.requests.get', return_value=_resp(json_data={'players': []})) as mock_get:
            p.fetch_ratings(['76561197993968023'], 'duel')
        assert mock_get.call_args[0][0] == 'http://qlstats.example/elo/bn/76561197993968023'

    def test_unsafe_rating_path_falls_back_to_elo(self):
        for bad in ('../admin', 'elo?x=1', 'elo bn', '//evil.example'):
            assert QlstatsProvider(extra={'rating_system': bad}).rating_system == 'elo'


# ---- slipgate --------------------------------------------------------

class TestSlipgate:
    def test_bulk_network_failure_raises(self):
        import requests
        p = SlipgateProvider(base_url='http://sg.example', api_key='sg_test')
        with patch('providers.slipgate.requests.post', side_effect=requests.ConnectionError()):
            with pytest.raises(requests.ConnectionError):
                p.fetch_ratings(['76561197993968023'], 'duel')

    def test_public_loop_stops_on_first_timeout(self):
        import requests
        p = SlipgateProvider(base_url='http://sg.example')
        ids = ['76561197993968023', '76561197960287930', '76561197960287931']
        with patch('providers.slipgate.requests.get', side_effect=requests.Timeout()) as mock_get:
            with pytest.raises(requests.Timeout):
                p.fetch_ratings(ids, 'duel')
        assert mock_get.call_count == 1

    def test_public_timeout_mid_loop_keeps_what_was_already_fetched(self):
        import requests
        p = SlipgateProvider(base_url='http://sg.example')
        ids = ['76561197993968023', '76561197960287930', '76561197960287931']
        found = _resp(json_data={'display': 1650, 'tier_name': 'Gold', 'mu': 18.0})
        with patch('providers.slipgate.requests.get',
                   side_effect=[found, requests.Timeout(), found]) as mock_get:
            result = p.fetch_ratings(ids, 'duel')
        # The rated player is kept; the loop stops at the timeout rather than
        # making the third player wait out the same one.
        assert list(result) == ['76561197993968023']
        assert mock_get.call_count == 2

    def test_public_http_error_skips_only_that_player(self):
        p = SlipgateProvider(base_url='http://sg.example')
        ids = ['76561197993968023', '76561197960287930', '76561197960287931']
        found = _resp(json_data={'display': 1650, 'tier_name': 'Gold', 'mu': 18.0})
        with patch('providers.slipgate.requests.get',
                   side_effect=[found, _resp(status_code=500), found]) as mock_get:
            result = p.fetch_ratings(ids, 'duel')
        assert list(result) == ['76561197993968023', '76561197960287931']
        assert mock_get.call_count == 3

    def test_public_http_errors_for_everyone_still_raise(self):
        """Nothing fetched and something failed is a failing source, not an
        empty roster: it must reach ranks_service to be logged and cached briefly."""
        import requests
        p = SlipgateProvider(base_url='http://sg.example')
        with patch('providers.slipgate.requests.get', return_value=_resp(status_code=500)):
            with pytest.raises(requests.HTTPError):
                p.fetch_ratings(['76561197993968023', '76561197960287930'], 'duel')

    def test_public_404_does_not_stop_the_loop(self):
        p = SlipgateProvider(base_url='http://sg.example')
        found = _resp(json_data={'display': 1650, 'tier_name': 'Gold', 'mu': 18.0})
        with patch('providers.slipgate.requests.get',
                   side_effect=[_resp(status_code=404), found]) as mock_get:
            result = p.fetch_ratings(['76561197993968023', '76561197960287930'], 'duel')
        assert mock_get.call_count == 2
        assert list(result) == ['76561197960287930']

    def test_gametype_aliases(self):
        p = SlipgateProvider()
        assert p.map_game_type('har') == 'harvester'
        assert p.map_game_type('dom') == 'domination'
        assert p.map_game_type('rr') == 'redrover'
        assert p.map_game_type('1f') == '1flag'
        assert p.map_game_type('duel') == 'duel'

    def test_unrated_gametype_is_none(self):
        p = SlipgateProvider()
        assert p.map_game_type('1fctf') is None
        assert p.map_game_type('ictf') is None

    def test_display_is_used_verbatim_and_tier_name_goes_to_title(self):
        p = SlipgateProvider(base_url='http://sg.example', api_key='sg_test')
        payload = {'ok': True, 'game_type': 'duel', 'rating_set': 'A', 'players': [
            {'steam_id': '76561197993968023', 'found': True, 'display': 2561,
             'tier_name': 'Elite', 'mu': 25.1, 'provisional': False},
        ]}
        with patch('providers.slipgate.requests.post', return_value=_resp(json_data=payload)):
            result = p.fetch_ratings(['76561197993968023'], 'duel')
        assert result['76561197993968023'] == {
            'rating': 25.1, 'display': '2561', 'provisional': False, 'title': 'Elite',
        }

    def test_unfound_player_is_skipped(self):
        p = SlipgateProvider(base_url='http://sg.example', api_key='sg_test')
        payload = {'ok': True, 'game_type': 'duel', 'rating_set': 'A', 'players': [
            {'steam_id': '76561197993968023', 'found': False, 'display': None},
        ]}
        with patch('providers.slipgate.requests.post', return_value=_resp(json_data=payload)):
            result = p.fetch_ratings(['76561197993968023'], 'duel')
        assert result == {}

    def test_bare_list_response_still_accepted(self):
        # Older/alternate deployments may still reply with a bare list
        # instead of the {"players": [...]} envelope -- both are accepted.
        p = SlipgateProvider(base_url='http://sg.example', api_key='sg_test')
        payload = [{'steam_id': '76561197993968023', 'found': True, 'display': 2561,
                    'tier_name': 'Elite', 'mu': 25.1, 'provisional': False}]
        with patch('providers.slipgate.requests.post', return_value=_resp(json_data=payload)):
            result = p.fetch_ratings(['76561197993968023'], 'duel')
        assert result['76561197993968023']['display'] == '2561'

    def test_malformed_bulk_body_returns_empty_not_crash(self):
        p = SlipgateProvider(base_url='http://sg.example', api_key='sg_test')
        payload = {'ok': True, 'game_type': 'duel', 'rating_set': 'A'}  # no 'players' key
        with patch('providers.slipgate.requests.post', return_value=_resp(json_data=payload)):
            result = p.fetch_ratings(['76561197993968023'], 'duel')
        assert result == {}

    def test_without_key_uses_public_per_player_endpoint(self):
        p = SlipgateProvider(base_url='http://sg.example')
        payload = {'display': 1650, 'tier_name': 'Gold', 'mu': 18.0, 'provisional': True}
        with patch('providers.slipgate.requests.get', return_value=_resp(json_data=payload)) as mock_get, \
             patch('providers.slipgate.requests.post') as mock_post:
            result = p.fetch_ratings(['76561197993968023'], 'duel')
        mock_post.assert_not_called()
        assert mock_get.call_args[0][0] == 'http://sg.example/api/v1/players/76561197993968023/ratings/duel'
        assert result['76561197993968023']['display'] == '1650'
        assert result['76561197993968023']['provisional'] is True

    def test_public_404_means_unranked(self):
        p = SlipgateProvider(base_url='http://sg.example')
        with patch('providers.slipgate.requests.get', return_value=_resp(status_code=404)):
            result = p.fetch_ratings(['76561197993968023'], 'duel')
        assert result == {}

    def test_429_raises_rate_limited_with_retry_after(self):
        p = SlipgateProvider(base_url='http://sg.example', api_key='sg_test')
        with patch('providers.slipgate.requests.post',
                   return_value=_resp(status_code=429, headers={'Retry-After': '42'})):
            with pytest.raises(RateLimited) as exc_info:
                p.fetch_ratings(['76561197993968023'], 'duel')
        assert exc_info.value.retry_after == 42

    def test_429_without_header_falls_back_to_default(self):
        p = SlipgateProvider(base_url='http://sg.example')
        with patch('providers.slipgate.requests.get', return_value=_resp(status_code=429)):
            with pytest.raises(RateLimited) as exc_info:
                p.fetch_ratings(['76561197993968023'], 'duel')
        assert exc_info.value.retry_after == 15


# ---- x76 (elo-service) ---------------------------------------------

class TestX76:
    A = '76561197993968023'
    B = '76561197960287930'

    def _provider(self, **extra):
        return ThunderdomeEloProvider(base_url='http://elo.example', api_key='key123', extra=extra)

    def test_map_game_type_is_identity(self):
        p = ThunderdomeEloProvider()
        assert p.map_game_type('ffa_auto') == 'ffa_auto'
        assert p.map_game_type('') is None

    def test_no_key_or_base_url_short_circuits_without_a_request(self):
        p = ThunderdomeEloProvider(base_url='http://elo.example')  # no api_key
        with patch('providers.elo_service.requests.get') as mock_get:
            result = p.fetch_ratings([self.A], 'ffa_auto')
        mock_get.assert_not_called()
        assert result == {}

    def test_one_bulk_request_for_the_whole_roster(self):
        payload = {self.A: {'sort_score': 1500}, self.B: {'sort_score': 1400}}
        with patch('providers.elo_service.requests.get',
                   return_value=_resp(json_data=payload)) as mock_get:
            result = self._provider().fetch_ratings([self.A, self.B], 'ffa_auto')
        assert mock_get.call_count == 1
        assert mock_get.call_args[0][0] == 'http://elo.example/players'
        assert mock_get.call_args.kwargs['params'] == {'ids': f'{self.A},{self.B}', 'mode': 'ffa_auto'}
        assert mock_get.call_args.kwargs['headers'] == {'X-API-Key': 'key123'}
        assert result[self.A]['display'] == '1500'
        assert result[self.B]['display'] == '1400'

    def test_sort_score_zero_falls_back_to_mu(self):
        """sort_score=0 means "not computed yet" -- must fall back to mu."""
        payload = {self.A: {'sort_score': 0, 'mu': 27.4, 'wins': 3, 'losses': 1}}
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider().fetch_ratings([self.A], 'ffa_auto')
        assert result[self.A]['display'] == '27.4'
        assert result[self.A]['rating'] == 27.4
        assert result[self.A]['title'] == '3-1'

    def test_null_entry_is_skipped(self):
        payload = {self.A: None, self.B: {'sort_score': 1400}}
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider().fetch_ratings([self.A, self.B], 'ffa_auto')
        assert list(result) == [self.B]

    def test_malformed_value_skips_only_that_player(self):
        payload = {self.A: {'sort_score': 'n/a'}, self.B: {'sort_score': 1400}}
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider().fetch_ratings([self.A, self.B], 'ffa_auto')
        assert list(result) == [self.B]

    def test_non_finite_value_skips_only_that_player(self):
        payload = {self.A: {'sort_score': 'nan'}, self.B: {'sort_score': 1400}}
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider().fetch_ratings([self.A, self.B], 'ffa_auto')
        assert list(result) == [self.B]

    def test_display_rank_label_shows_the_label(self):
        payload = {self.A: {'sort_score': 1500, 'rank_label': ' Gold II '}}
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider(display='rank_label').fetch_ratings([self.A], 'ffa_auto')
        assert result[self.A]['display'] == 'Gold II'
        assert result[self.A]['rating'] == 1500.0

    def test_display_rank_label_falls_back_to_the_number_when_blank(self):
        payload = {self.A: {'sort_score': 1500, 'rank_label': '  '}, self.B: {'sort_score': 1400}}
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider(display='rank_label').fetch_ratings([self.A, self.B], 'ffa_auto')
        assert result[self.A]['display'] == '1500'
        assert result[self.B]['display'] == '1400'

    def test_default_display_ignores_the_label(self):
        payload = {self.A: {'sort_score': 1500, 'rank_label': 'Gold II'}}
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider().fetch_ratings([self.A], 'ffa_auto')
        assert result[self.A]['display'] == '1500'

    def test_rank_label_carries_its_tier_color(self):
        payload = {
            self.A: {'sort_score': 1500, 'rank_label': 'Gold III'},
            self.B: {'sort_score': 1900, 'rank_label': 'platinum IV'},
        }
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider(display='rank_label').fetch_ratings([self.A, self.B], 'ffa_auto')
        assert result[self.A]['color'] == 'yellow'
        assert result[self.B]['color'] == 'cyan'  # tier match ignores case

    @pytest.mark.parametrize('label,color', [
        ('Nab', 'white'), ('Bronze II', 'yellow'), ('Silver I', 'white'), ('Gold III', 'yellow'),
        ('Platinum IV', 'cyan'), ('Diamond I', 'blue'), ('Prism', 'magenta'), ('LIGHT', 'green'),
    ])
    def test_every_tier_has_a_color(self, label, color):
        payload = {self.A: {'sort_score': 1500, 'rank_label': label}}
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider(display='rank_label').fetch_ratings([self.A], 'ffa_auto')
        assert result[self.A]['color'] == color

    def test_no_color_without_a_recognised_tier_label(self):
        payload = {
            self.A: {'sort_score': 1500, 'rank_label': 'Goldfish'},  # not the Gold tier
            self.B: {'sort_score': 1400, 'rank_label': '  '},        # falls back to the number
        }
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider(display='rank_label').fetch_ratings([self.A, self.B], 'ffa_auto')
        assert result[self.A]['color'] is None
        assert result[self.B]['color'] is None

    def test_no_color_when_showing_the_score(self):
        payload = {self.A: {'sort_score': 1500, 'rank_label': 'Gold III'}}
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider().fetch_ratings([self.A], 'ffa_auto')
        assert result[self.A]['color'] is None

    def test_404_means_no_ratings(self):
        with patch('providers.elo_service.requests.get', return_value=_resp(status_code=404)):
            assert self._provider().fetch_ratings([self.A], 'ffa_auto') == {}

    def test_network_failure_raises(self):
        import requests
        with patch('providers.elo_service.requests.get', side_effect=requests.ConnectionError()):
            with pytest.raises(requests.ConnectionError):
                self._provider().fetch_ratings([self.A], 'ffa_auto')

    def test_server_error_raises(self):
        import requests
        with patch('providers.elo_service.requests.get', return_value=_resp(status_code=500)):
            with pytest.raises(requests.HTTPError):
                self._provider().fetch_ratings([self.A], 'ffa_auto')

    def test_ids_not_asked_for_are_ignored(self):
        payload = {self.A: {'sort_score': 1500}, '76561197960287999': {'sort_score': 9}}
        with patch('providers.elo_service.requests.get', return_value=_resp(json_data=payload)):
            result = self._provider().fetch_ratings([self.A], 'ffa_auto')
        assert list(result) == [self.A]


# ---- registry ------------------------------------------------------------

def test_server_status_source_is_gone():
    from providers import BUILTIN_PROVIDERS
    assert set(BUILTIN_PROVIDERS) == {'qlstats', 'slipgate', 'elo_service'}
