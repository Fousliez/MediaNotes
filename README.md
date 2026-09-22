# MediaNotes

Desktopová aplikace pro vlastní knihovnu obrázků, GIFů a krátkých videí s popisky a poznámkami.

## Co umí

- přidávání médií přes dialog i drag & drop,
- JPG, PNG, WebP, BMP, GIF a běžná video rozšíření,
- náhledová galerie,
- animované GIFy,
- krátký popisek a delší poznámka,
- vlastní kategorie vlevo,
- vytváření, přejmenování, mazání a změna pořadí kategorií,
- přesouvání jednoho i více médií mezi kategoriemi,
- pravé tlačítko nad kategoriemi i médii,
- vyhledávání v názvech, popiscích, poznámkách a kategoriích,
- SQLite databáze,
- původní soubory se nemažou ani nekopírují.

## Spuštění

```bash
git clone https://github.com/Fousliez/MediaNotes.git
cd MediaNotes
chmod +x start_app.sh
./start_app.sh
```

Pokud už aplikaci máš:

```bash
cd ~/MediaNotes
git pull
./start_app.sh
```

Databáze je lokálně v:

```
data/media_notes.db
```

## Zkratky

- `Ctrl+O` – přidat média
- `Ctrl+S` – uložit změny
- `Ctrl+F` – vyhledávání
- `Delete` – smazat vybrané položky z databáze
