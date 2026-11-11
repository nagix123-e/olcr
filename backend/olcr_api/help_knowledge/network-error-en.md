# Network versus processing failures

NETWORK_ERROR means the frontend could not obtain a usable network result. It can reflect backend exceptions or connection state, not only internet failure. With HTTP_500/502, inspect the accompanying processing code; AGENT_RESPONSE_SAVE_FAILED is answer persistence rather than model connectivity. Check history and run state before repeating a request because the user message may already be saved.
