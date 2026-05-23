# Gree Central LAN

`gree_central_lan` is a Home Assistant custom integration for Gree central air-conditioning systems that expose one main LAN controller with multiple indoor units.

This repository is the standalone source of truth for the integration. Home Assistant should install and update it from this repository through HACS instead of from a checked-out development workspace.

Current goals of this implementation:

- Config-entry setup instead of legacy YAML platform config
- Guided UI flow to name each indoor unit and bind a Home Assistant temperature sensor
- Show the selected external temperature sensor inside the climate card as `current_temperature`
- Use a long-lived UDP socket and packet-driven state updates instead of periodic polling

The integration lives under [`custom_components/gree_central_lan`](custom_components/gree_central_lan).

## Installation

Add `https://github.com/Crazykev/gree-central-lan` to HACS as a custom repository of type `Integration`, install `Gree Central LAN`, then restart Home Assistant.
