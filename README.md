# MediaNotes

Jednoduchá desktopová aplikace pro vlastní knihovnu obrázků, GIFů a krátkých videí.

## Funkce první verze

- kategorie vlevo,
- náhledy médií vpravo,
- krátký popisek,
- delší poznámka,
- ukládání metadat do SQLite,
- přidávání více souborů najednou,
- náhled obrázků,
- přehrávání GIFů v náhledu,
- videa eviduje a otevře v systémovém přehrávači,
- původní soubory nemaže ani nekopíruje, ukládá pouze jejich cestu.

## Spuštění

```bash
git clone https://github.com/Fousliez/MediaNotes.git
cd MediaNotes
chmod +x start_app.sh
./start_app.sh
```

Při prvním spuštění se vytvoří virtuální prostředí a nainstaluje PySide6.

Databáze se vytvoří automaticky v:

```
data/media_notes.db
```
