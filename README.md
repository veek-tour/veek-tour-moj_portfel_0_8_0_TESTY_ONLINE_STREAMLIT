# Mój Portfel 0.8.0 — TESTY ONLINE (Streamlit Community Cloud)

**Wersja demonstracyjna do sprawdzania na zmyślonych transakcjach.** Nie zastępuje lokalnej 0.7.0. Zawiera kod w Pythonie, interfejs Streamlit, bazę SQLite i testy.

## Bezpieczeństwo danych

- **Nie wpisuj prawdziwych zakupów, kwot ani danych rachunków i nie przesyłaj prawdziwej bazy danych/CSV.** Publiczny adres testowej aplikacji może umożliwiać innym przeglądanie lub zmianę wpisów.
- W Streamlit Community Cloud lokalna baza SQLite aplikacji **nie gwarantuje trwałości**: dane mogą zniknąć po restarcie lub nowym wdrożeniu.
- Nie dodawaj do repozytorium plików `.sqlite3`, folderu `data/`, `secrets.toml` ani eksportów z realnym portfelem.
- `worker.py` nie działa automatycznie w chmurze. Korzystaj z przycisku **„Pobierz najnowsze ceny i kursy”** w zakładce „Wyceny i historia”.

## Publikacja na Streamlit Community Cloud

1. Rozpakuj archiwum `moj_portfel_0_8_0_TESTY_ONLINE_STREAMLIT.zip` na komputerze.
2. Prześlij **zawartość** ZIP-a na GitHub do istniejącego repozytorium testowego albo do nowego repozytorium. Pliki `app.py`, `requirements.txt`, `.streamlit/` i folder `portfolio/` muszą znajdować się bezpośrednio w katalogu głównym repozytorium.
3. Jeżeli aktualizujesz stare repozytorium, usuń pozostałości po starszych plikach, których nie ma już w ZIP-ie. **Nie usuwaj cudzych lub własnych plików z innym przeznaczeniem.**
4. W Streamlit Community Cloud wybierz repozytorium i `Main file path: app.py`. Przy istniejącym wdrożeniu odczekaj na uruchomienie nowej wersji i ewentualnie wybierz „Reboot app” w zarządzaniu aplikacją.
5. Sprawdź, czy tytuł wskazuje **0.8.0 — TESTY ONLINE**, a następnie testuj tylko fikcyjne transakcje.

## Co nowego w 0.8.0

- Opcjonalna wpłata przy zakupie z kwotą kosztu wraz z prowizją (lub własną). Edycja zakupu aktualizuje kwotę wpłaty tylko, jeśli wybrano podążanie za zakupem; usunięcie zakupu usuwa jego powiązaną wpłatę. Inne wpłaty pozostają.
- `Nagroda` ze stakingu tworzy darmowe jednostki aktywa (bez gotówki); `Nagroda` z obligacji tworzy wypłatę odsetek w gotówce. Notatka jest obowiązkowa.
- Obligacje są szacunkowo wyceniane liniowo na podstawie rocznej stopy, bez kapitalizacji. Faktyczny wykup rejestrujesz jako sprzedaż wybranej transzy, wpisując całą kwotę otrzymaną z wykupu. Nie księguj drugi raz tych samych odsetek.
- Akceptowane tickery EIMI (USD, Yahoo EIMI.L) i IWQU (USD, Yahoo IWQU.L). Starsze, rzeczywiste wpisy EUR zachowywane są w bazie bez cichego przewalutowania.
- Ulepszona historia operacji (Lp., filtry, szczegóły i notatki), lista instrumentów (ostatnia cena, zmiana dnia, data i źródło), docelowy podział portfela według kategorii, przycisk odświeżania historii.
- Referencyjna cena historyczna ETF/krypto służy do porównania, nie jest ceną faktycznego zakupu.
- Awaryjne ceny krypto USD z Yahoo w razie niedostępności Binance.

**Znane ograniczenia:** historyczne srebro nadal może używać wyraźnie oznaczonych notowań futures, nie spot; nie ma jeszcze kompletnych zapasowych źródeł cen wszystkich ETF-ów ani pełnego modułu `Decimal`. Nie ma okienka modalnego kopiującego TradingView. Ceny, w tym kursy walut, mogą być chwilowo niedostępne. Nie oznacza to automatycznie zera lub poprawnej bieżącej wyceny.

## Testy

Z poziomu katalogu projektu (jeżeli masz zainstalowane biblioteki z `requirements.txt`):

```bash
python -m pytest -q
```

Przed spakowaniem: **106 testów Pythona przechodzi**. W obecnym środowisku nie udało się uruchomić rzeczywistego interfejsu Streamlit (brak zainstalowanego Streamlit i brak możliwości pobrania zależności); widoki trzeba sprawdzić po publikacji online. To **wersja testowa, nie zatwierdzona do rzeczywistego księgowania inwestycji**.

Zobacz również `SCENARIUSZE_TESTOWE_080.md` i `CHANGELOG.md`.
