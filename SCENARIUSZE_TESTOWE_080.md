# Scenariusze ręcznego testowania — 0.8.0 TESTY ONLINE

Testuj na **fikcyjnych danych**. Na początek załóż testowy rachunek i uruchom pobieranie cen oraz kursów. Jeśli usługa notowań nie odpowiada, nie traktuj braku ceny jako błędu obliczeń i nie podawaj fałszywych kursów rzeczywistego portfela.

## 1. Zakup z wpłatą

- Wejdź w **Operacje → Dodaj operację**; wybierz krypto BTC, rachunek testowy, datę, ilość 1, cenę 100 USD i prowizję 5 USD.
- Zaznacz **„Dodaj jednocześnie wpłatę gotówki na rachunek”**. Oczekiwana propozycja: **105 USD**.
- Zapisz. W historii powinny powstać powiązane wpisy: zakup i wpłata. Saldo gotówki w USD po tych operacjach: **0 USD**, o ile wcześniej było 0.
- Zmień koszt zakupu na 120 USD: jeśli wpłata była ustawiona jako automatycznie podążająca za zakupem, oczekiwana kwota: **125 USD**. Ręcznie zmieniona kwota wpłaty nie powinna zostać nadpisana przez późniejszą edycję zakupu.
- Usuń zakup: powiązana wpłata powinna zniknąć, ale każda niezależnie wprowadzona wpłata powinna pozostać.

## 2. Nagroda stakingowa

- Dodaj **Nagroda → Staking**, wybierz BTC i wpisz **0,1 BTC** oraz wymaganą notatkę „Nagroda za staking”.
- Powinna wzrosnąć liczba BTC, ale **łączny koszt nabycia i kapitał wniesiony nie mogą wzrosnąć**. Wycena rynkowa darmowych jednostek zwiększa wynik (jeśli jest dostępna cena BTC).

## 3. Obligacje i wypłacone odsetki

- Dodaj osobną transzę obligacji: 10 jednostek po 100 PLN, założone oprocentowanie 10% rocznie, termin wykupu późniejszy niż rok od zakupu.
- Szacunkowa wartość po około pół roku powinna wynosić około **1 050 PLN** (różnice wynikają z liczby dni), bez odsetek od odsetek.
- Dodaj **Nagroda → Wypłacone odsetki obligacji**, wskaż konkretną transzę i wpisz **30 PLN** wraz z notatką.
- Saldo gotówki powinno wzrosnąć o 30 PLN, ale suma gotówki i wyceny obligacji nie może wzrosnąć drugi raz od tej samej wypłaty.
- Przy finalnym wykupie dodaj sprzedaż danej transzy i wpisz **całą faktycznie otrzymaną kwotę**. Nie zapisuj tych samych odsetek drugi raz jako nagrody.

## 4. Lista instrumentów i historia

- W zakładce „Aktywa i rachunki” sprawdź, czy **EIMI i IWQU** wyświetlają walutę **USD**, a pozostałe trzy ETF-y EUR. Sprawdź źródło i datę notowania.
- W historii operacji sprawdź numerację **Lp. 1, 2, 3…**, filtry i rozwijane szczegóły. Po usunięciu wpisu numeracja Lp. powinna być bez luk, choć techniczne ID nie zmienia się.
- Przy wpisywaniu zakupu ETF/krypto sprawdź, czy historyczna cena referencyjna nie zastępuje Twojej ręcznej ceny.

## 5. Historia i docelowa struktura

- Wejdź do „Wyceny i historia”; użyj **„Odśwież historię portfela”**. Wykres powinien pokazać również jedną datę jako punkt, jeśli istnieje tylko jeden dzień danych.
- W „Wyniki i benchmark” zapisz docelowe udziały np. **ETF 60%, Metale 20%, Kryptowaluty 10%, Obligacje 10%** (kategorie muszą odpowiadać dokładnym nazwom w formularzu, reszta 0%). Suma musi wynosić 100%.
- Porównaj wartości rzeczywiste z docelowymi. Funkcja niczego nie kupuje ani nie sprzedaje.

## 6. Kontrola danych

- Wejdź do „Kontrola danych”, uruchom diagnostykę. Nowy typ „REWARD” nie może być zgłaszany jako nieobsługiwany.
- W razie błędu zrób zrzut ekranu z opisem wybranej operacji. Nie podawaj danych prawdziwych inwestycji.
