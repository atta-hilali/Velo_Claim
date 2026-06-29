from abc import ABC, abstractmethod

from velo_claim.core.models import PayerRuleSet


class PayerRuleLoaderInterface(ABC):
    @abstractmethod
    def load(self, payer_id: str, plan_id: str) -> PayerRuleSet: ...
