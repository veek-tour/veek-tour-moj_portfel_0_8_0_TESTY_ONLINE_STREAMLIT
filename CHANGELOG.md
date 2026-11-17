# Historia zmian

## 0.8.0 — TESTY ONLINE — operacje, obligacje, wyceny i wygoda

**Wersja eksperymentalna do fikcyjnych danych; lokalna wersja Windows 0.7.0 nie została zmieniona.**

- Nowa operacja „Nagroda”: darmowe jednostki kryptowaluty ze stakingu (zerowy koszt, bez dodatkowej wpłaty); wypłacone odsetki obligacji dopisywane jako wpływ gotówki przypisany do transzy, bez podwójnego liczenia szacunkowej wyceny.
- Obligacje: szacunkowa wycena liniowa według ustalonej stopy rocznej, bez kapitalizacji; każda transza oddzielnie, jej parametry można edytować. Przy wykupie podaje się faktycznie otrzymaną kwotę.
- Zakup: opcjonalna powiązana wpłata gotówki. Propozycja obejmuje koszt zakupu i prowizję, można wpisać własną kwotę. Zakup i wpłata zapisywane atomowo, synchronizacja lub bezpieczne usuwanie powiązanych rekordów.
- Historia: kolejna numeracja „Lp.” niezależna od ukrytego ID, filtry, rozwijane szczegóły, notatki; przyciski odświeżania historii, wykres pokazujący również pojedynczy dzień.
- Lista instrumentów: ostatnia cena, waluta, data, źródło i dzienna zmiana na podstawie porównywalnych notowań; filtrowanie według kategorii.
- ETF-y EIMI i IWQU: wpisy USD / giełda londyńska, symbole źródeł EIMI.L i IWQU.L. Historyczne zapisy EUR są zachowywane; aplikacja nie przelicza ich automatycznie na USD.
- Informacyjna historyczna cena referencyjna dla ETF/krypto obok ręcznie wprowadzonej ceny transakcyjnej; nie uzupełnia pola automatycznie.
- Docelowe udziały kategorii: konfiguracja sumująca się do 100% i porównanie z bieżącą strukturą bez automatycznego handlu.
- Krypowaluty: awaryjne notowania Yahoo w USD, gdy źródło Binance nie odpowiada, z oznaczeniem źródła; dalsza rozbudowa źródeł cen pozostaje na liście prac.
- Schemat bazy 9 z kopią przed migracją, import CSV zachowujący odwołania do powiązanych wpłat, dodatkowe testy regresyjne. `pytest`: 106 testów zaliczonych.

**Nie jest jeszcze wdrożone:** przejście wszystkich rozliczeń na Decimal, oddzielenie katalogu danych lokalnej wersji Windows od aplikacji, pełna kontrola GUI w działającym Streamlit Cloud, kompleksowy fallback dla ETF-ów i historyczne ceny spot srebra zamiast futures. Nowy formularz jest uporządkowany, ale nie jest modalnym oknem w stylu TradingView.


## 0.7.0 — dynamiczne operacje, waluty, fizyczne metale

- Jeden wspólny formularz operacji z dynamicznymi zestawami pól dla instrumentów standardowych, metali, obligacji, stakingu oraz operacji gotówkowych/transferowych.
- PLN/EUR/USD: kursy NBP dopasowywane do daty, nigdy z przyszłości; data i źródło kursu zapisane przy transakcji. W szczegółach można wprowadzić faktyczny kurs i kwotę rozliczenia brokera.
- Automatyczna kwota transakcji z liczby i ceny oraz domyślna prowizja 0. Zapis ostatnio używanego rachunku; podsumowanie rozliczenia przed zapisem.
- Kursy EUR/PLN i USD/PLN wraz z datą publikacji NBP w panelu bocznym oraz ręczne odświeżenie.
- Fizyczne złoto i srebro: osobno cena referencyjna PLN/oz i podpisana korekta PLN/oz. Po zakupie wycena po wartości samego kruszcu; rzeczywiste koszty zawierają korektę.
- Złoto wyceniane preferencyjnie z NBP, srebro z bieżących notowań spot Gold API; historyczne futures są jawnie oznaczonym przybliżeniem awaryjnym.
- Migracja bazy do schematu 8 z kopią zapasową. Zachowano dotychczasowe rekordy i zgodność starego sposobu edycji.
- Plik uruchomieniowy Windows korzysta ze wspólnego środowiska zależności, żeby nie pobierać bibliotek przy każdym rozpakowaniu ZIP.
- Dodano testy obliczeń premii metalowej, ścieżki NBP, historii FX, kosztu przewalutowania i migracji.


## 0.6.2

- Dodano osobną tabelę transz obligacji powiązaną z konkretną operacją zakupu. Każdy zakup jest oddzielną transzą, także gdy kilka zakupów tego samego instrumentu ma tę samą datę.
- Warunki oprocentowania, marża, nominał, termin wykupu i kapitalizacja prognozy są zapisywane niezależnie dla każdej transzy. Dane w kartotece instrumentu służą wyłącznie jako wartości domyślne kolejnych zakupów.
- Dla transz indeksowanych inflacją przyjęta stopa jest indywidualna. Jej zmiana przelicza całą historię wybranej transzy od zakupu; nie zmienia innych transz. Edycja pozostałych warunków transzy również uruchamia ponowne przeliczenie historii.
- Wartość obligacji jest sumą wycen poszczególnych transz. Naliczanie odsetek kończy się na terminie wykupu; prognoza nie rośnie po tej dacie.
- Dodano wybór konkretnej transzy przy nowej sprzedaży lub wykupie obligacji oraz kontrolę dostępnej ilości. Starsze sprzedaże bez wskazania transzy pozostają rozdzielane proporcjonalnie, a nie metodą FIFO.
- Kalendarz pokazuje terminy wykupu osobno dla transz, a kontrola danych sprawdza ich kompletność i poprawność.
- Migracja starszej bazy tworzy kopię przed zmianą schematu i zakłada jedną transzę na każdy wcześniejszy zakup obligacji. Potwierdzona wcześniej globalna inflacja jest kopiowana do zmigrowanych transz indeksowanych.
- Eksport/import CSV zachowuje warunki transz i mapuje odwołania sprzedaży do zakupów również po przeniesieniu danych do innej instalacji.
- Dodano testy niezależności transz, retroaktywnego przeliczania inflacji, wykupów, migracji i przenoszenia CSV.

## 0.6.1

- Dywidendy i odsetki wymagają teraz przypisania do konkretnego aktywa również w silniku księgowym; niekompletne operacje są odrzucane zamiast trafiać do nieprzypisanych przychodów.
- Benchmark SPY korzysta z liniowego przejścia przez uporządkowane daty cen i kursów walut zamiast ponownego przeszukiwania całej historii dla każdego dnia. Zachowano dotychczasową metodę wyceny i obsługę przepływów.
- Dodano testy wymagania przypisania przychodów oraz zgodności historycznej serii benchmarku po optymalizacji.

## 0.6.0

- Jeden dynamiczny formularz dodawania operacji; wybór rodzaju transakcji i walut rozliczenia aktualizuje widoczne pola oraz domyślne kursy.
- Szczegóły instrumentu pokazują średni koszt zakupu w walucie instrumentu i w PLN, wartość, wynik oraz przypisane dywidendy, odsetki i rzeczywiście otrzymane nagrody.
- Analiza wyników ograniczona do całego portfela i kategorii; usunięto ranking wyników rachunków/platform.
- Wykres historii portfela pokazuje wartość portfela oraz skumulowany kapitał wpłacony netto.
- Ceny kryptowalut odświeżane co 6 godzin, pozostałe notowane instrumenty raz na dobę; FX i SPY mają osobny cykl dobowy.
- Dodano ponawianie prób pobierania danych, zapasową ścieżkę pobierania w Yahoo Finance, zachowywanie ostatniej poprawnej ceny i oznaczanie niekompletnych wycen.
- Obligacje wyceniane uproszczonym modelem oprocentowania i jednej przyjętej inflacji; bez zewnętrznych notowań i scenariuszy inflacyjnych.
- Staking pokazuje zapisane APY; prognozowane nagrody nie są tworzone ani księgowane. Rzeczywiste nagrody rejestruje się jako operacje przypisane do aktywa.
- Dodano notatki (z edycją i usuwaniem) oraz kalendarz z własnymi wydarzeniami i automatycznymi przypomnieniami.
- Rozszerzono kontrolę integralności bazy, powiązań, księgi, instrumentów, obligacji, cen, kursów, kalendarza i migawek.
- Automatyczne kopie zapasowe raz dziennie, walidacja przywracanej bazy oraz rozszerzona obsługa importu CSV po nazwie rachunku.
- Aplikacja jest skonfigurowana wyłącznie do dostępu lokalnego przez `127.0.0.1`; usunięto konfigurację Docker/NAS.
- Dodano testy nowych funkcji i korekt harmonogramu aktualizacji.


## 0.6.3 — instrumenty wstępnie zdefiniowane
- Dodano 16 przygotowanych instrumentów do każdej nowej bazy; dodawanie jest idempotentne i nie nadpisuje istniejących instrumentów.
- Dla istniejącej bazy tworzona jest kopia zapasowa przed pierwszym uzupełnieniem brakujących instrumentów.
- Usunięto formularz ręcznego dodawania instrumentów oraz ręczną konfigurację symbolu źródłowego z interfejsu.
- Lista instrumentów pokazuje komplet podstawowych pól: ticker, nazwę, kategorię, walutę, rynek, źródło notowań, jednostkę oraz ISIN/notatki.
- Usunięto nieaktualne komunikaty sugerujące ręczne dodawanie kryptowalut w zakładce stakingu i operacji.
- Dodano pobieranie dziennych i bieżących cen krypto przez publiczne Binance Spot Market Data API (bez klucza API); USDT jest traktowany jak USD.
- Złoto i srebro ewidencjonowane są w uncjach trojańskich; ETF-y zapisano jako warianty akumulacyjne (Acc) z ISIN-ami w notatkach.
