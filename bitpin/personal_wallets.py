"""Reviewed public Ethereum associations; no current ownership claims.

Immutable metadata only. Balances and first observations belong to collector
receipts, never to this attribution catalog. See ADDITIONAL_PERSONAL_WALLETS.md.
"""
from types import MappingProxyType


WALLETS = (
    MappingProxyType({
        'address': '0x220866b1a2219f40e72f5c628b65d54268ca3a9d',
        'person': 'Vitalik Buterin', 'label': 'Vb 3', 'kind': 'Ethereum Safe',
        'attribution': MappingProxyType({
            'person': 'Vitalik Buterin', 'label': 'Vb 3',
            'kind': 'public_explorer_attribution', 'checked_date': '2026-10-11',
            'current_ownership_verified': False,
            'address_source': 'https://etherscan.io/address/0x220866b1a2219f40e72f5c628b65d54268ca3a9d',
            'profile_source': 'https://info.arkm.com/research/vitalik-buterin-net-worth',
            'association_strength': 'public_attribution',
            'association': 'Etherscan Vb 3 label and published individual-holder research.',
            'limitations': 'Current control unverified; no complete inventory of personal wallets or assets.',
            'personal_funds_verified': False,
        }),
    }),
    MappingProxyType({
        'address': '0xf6b6f07862a02c85628b3a9688beae07fea9c863',
        'person': 'Patricio Worthalter', 'label': 'Historical council address', 'kind': 'Ethereum address',
        'attribution': MappingProxyType({
            'person': 'Patricio Worthalter', 'label': 'Historical council address',
            'kind': 'official_historical_association', 'checked_date': '2026-10-11',
            'current_ownership_verified': False,
            'address_source': 'https://docs.arbitrum.foundation/foundational-documents/transparency-report-initial-foundation-setup',
            'profile_source': 'https://docs.arbitrum.foundation/foundational-documents/transparency-report-initial-foundation-setup',
            'association_strength': 'strong_historical',
            'association': 'Arbitrum Foundation lists this address for Patricio in its initial Security Council March cohort.',
            'limitations': 'Historical association only. Current control and whether funds are personal remain unverified.',
            'personal_funds_verified': False,
        }),
    }),
    MappingProxyType({
        'address': '0x6cd66dbdfe289ab83d7311b668ada83a12447e21',
        'person': 'Arthur Hayes', 'label': 'Arthur Hayes 4', 'kind': 'Ethereum address',
        'attribution': MappingProxyType({
            'person': 'Arthur Hayes', 'label': 'Arthur Hayes 4',
            'kind': 'public_explorer_attribution', 'checked_date': '2026-10-11',
            'current_ownership_verified': False,
            'address_source': 'https://etherscan.io/address/0x6cd66dbdfe289ab83d7311b668ada83a12447e21',
            'profile_source': 'https://info.arkm.com/research/arthur-hayes-net-worth-crypto-holdings-2026',
            'profile_published_date': '2026-06-19', 'association_strength': 'medium_public',
            'association': 'Etherscan Arthur Hayes 4 label; Arkham describes the publicly attributed holdings profile.',
            'limitations': 'No self-disclosure found for this address. Current control unverified; personal versus fund capital is ambiguous.',
            'personal_funds_verified': False,
        }),
    }),
)
ADDRESSES = frozenset(wallet['address'] for wallet in WALLETS)
