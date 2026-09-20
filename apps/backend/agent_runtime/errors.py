class AgentRuntimeError(RuntimeError):
    """Base class for deterministic agent-runtime failures."""


class DuplicateJobError(AgentRuntimeError):
    pass


class AgentJobNotFoundError(AgentRuntimeError):
    pass


class AgentConflictError(AgentRuntimeError):
    pass


class AgentContractError(AgentRuntimeError):
    pass


class ProviderUnavailableError(AgentRuntimeError):
    pass
