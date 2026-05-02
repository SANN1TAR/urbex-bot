# Project: Urbex Bot

## Overview

Telegram bot for discovering abandoned/urbex locations in Russian and CIS cities.
Bot username: @TZRCTfinderBot

## Purpose

Help urban explorers find abandoned buildings, factories, hospitals, and other
derelict structures in their city. Interface is Tinder-style: one object at a time,
swipe through with buttons.

## Target Audience

Urban explorers (urbex community) in Russia and CIS countries. Users who search
for abandoned places to visit. Age: 18–35, familiar with Telegram bots.

## Core Features

1. **City-based browsing** — register once with your city, get objects from that city
2. **Tinder interface** — one object at a time, "Next" / "Restart" buttons
3. **Object card** — name + coordinates/address + photo
4. **Search by name** — find a specific object by name (uses LLM)
5. **City switching** — change city at any time
6. **7-day cache** — objects cached in SQLite, refreshed automatically

## What We Do NOT Do

- No roofing (крыши) or diggers (диггеры) — only surface abandoned objects
- No currently operating buildings (restaurants, offices, malls)
- No government/military facilities
- No construction sites or renovation objects
- No map view (only coordinates/address in text)
- No user-submitted objects
- No social features (likes, comments, sharing)

## Known Limitations (as of v0.5)

- SQLite DB resets on Railway redeploy (ephemeral storage) — all cached objects lost
- _shown_global is in-memory — shown history resets on restart
- Coordinates missing for many objects (urban3p.ru hides them behind POST form)
- Data from urban3p.ru may be outdated (demolished objects still listed)
- Nominatim geocoding poorly resolves industrial abandoned buildings by name
