"""One task owns one immutable provider-settings snapshot."""

from copy import deepcopy

import httpx

from stock_god.config import AppConfig

from .charts import Charts
from .common import Transport
from .evidence import Evidence
from .funds import Funds
from .news import News
from .meoz import MeozProvider
from .prediction_inputs import PredictionInputs
from .quotes import Quotes
from .text_analysis import TextAnalysis
from .themes import Themes


class MarketServices(Quotes, Charts, Evidence, News, PredictionInputs, TextAnalysis, Themes, Funds):
    def __init__(self, config: AppConfig, settings: dict | None = None, *, client: httpx.Client | None = None):
        self.config = config
        self.settings = deepcopy(settings or {})
        self.http = Transport(self.settings, client)
        self._injected_client = client
        self.meoz = MeozProvider(self.settings, self.http.client)

    def meoz_request(self, apiname, params, fields=None, *, history=False, deadline=None):
        return self.meoz.request(apiname, params, fields, history=history, deadline=deadline)

    def meoz_ticks(self, day, symbols, *, final=False, deadline=None):
        return self.meoz.ticks(day, symbols, final=final, deadline=deadline)

    def with_settings(self, settings: dict):
        return MarketServices(self.config, settings, client=self._injected_client)

    def close(self):
        self.http.close()
