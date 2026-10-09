# 1. Product contract and release boundary

## Purpose

Build an evidence-first investment decision-support application for self-directed users. The central question is: given the user's declared goal, account state, approved policy and permitted data, what should the user investigate, hold, buy, reduce, sell or leave unchanged, and why? Frequent trading signals are required but are not a quota of trades. A justified abstention is a valid outcome.

The personal pilot and near-term commercial product share an independent application core. Both a complete self-hosted edition and a hosted multi-user edition are required at first release. The application is intended to be open source under Apache-2.0; the descriptive working title is not a selected brand. Hosted revenue pays for managed operation, not privileged investment rankings.

## Mandatory release scope

US-listed stocks and ETFs are the mandatory discovery universe. Accounts and reporting are currency-aware, initially including CAD and USD. Canadian and US residents are both in the commercial launch scope, subject to product-specific legal clearance. Imported instruments outside recommendation eligibility remain represented; absent data is an explicit limitation, never zero exposure.

First release includes policy onboarding; portfolio snapshots and transaction journals; manual and CSV workflows; named read-only integrations; watchlist CRUD; monitored events; all six strategy/research families; independent AI-originated proposals and a separate evaluator; opt-in options; user-facing virtual portfolios; cash-flow-aware benchmarking; governed chat; evidence/provenance; all six improvement areas including actual code and optimizer recursion; runtime human promotion/adoption; migration, deletion and recovery.

The six stock/ETF families are allocation/rebalancing, quality/valuation, income research, long-only swing/position trend-momentum, regular-session long-only opening-range breakout, and independent AI theses. Options add long calls/puts, covered calls, cash-secured puts and defined-risk vertical spreads. This is not permission to postpone the difficult families as placeholders.

## Free baseline and qualified capability profiles

The free-data/rules-only profile must deliver portfolio-specific allocation/rebalancing and at least one qualified non-intraday long-only strategy without paid market data or paid inference. A supported free provider account can be required. Hosting, optional connectors, premium data and existing model subscriptions are separate costs. No claim of free universal live consolidated stock/option coverage is permitted. [SRC-01, SRC-02]

Feature presence, operational qualification, investment evidence and current user eligibility are separate concepts. Catalogue inclusion does not authorize action. Every family must have a functioning versioned implementation and evaluated status; a failed variant stays research-only. For the complete release, demonstrate the free floor and qualified configurations for required intraday and live options use. Turning off every substantive strategy does not satisfy the release requirement.

## Exclusions and non-authority

No broker order submission, amendment, cancellation, discretionary custody or autonomous execution. No required sub-minute scalping, extended-hours actionable trades, native mobile clients, SMS, full tax-return engine, automated tax-loss harvesting, unlimited AI bundle, mandatory GPU/Kubernetes, unrestricted hosted arbitrary-code strategy builder, or automatic Yahoo account synchronization. Stock proposals are cash-funded long-only, nonleveraged and noninverse. This does not suppress the approved options catalogue or existing unsupported exposures.

A model cannot expand these permissions. The user's acceptance of planning recommendations is not an investment-policy signature, a software-promotion authorization, a provider payment approval or permission to share private learning.

## Product success measures

Primary: reconciled financial correctness; supported material claims; meaningful portfolio-specific decisions; timely invalidation; suitable abstention; prospective evaluation integrity; safe degradation; user control. Secondary: workflow completion, relevant alert rates, explanatory usefulness, cost and operational reliability. Do not optimize trades per day, user clicks, recommendation approval or recent P&L as the sole objective.

All acceptance targets are requirements to test, not achieved results. Implementation waves are dependency order inside the same release. External permissions, legal clearance and resource feasibility remain named gates, not preference questions to reopen indefinitely.
