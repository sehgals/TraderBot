DEFAULT_STRATEGY_TYPE = "managed_dynamic_reentry"


class Strategy:
    strategy_type = None

    def __init__(self, client, config, state, clock=None):
        self.client = client
        self.config = config
        self.state = state
        self.clock = clock

    def run_once(self):
        raise NotImplementedError
