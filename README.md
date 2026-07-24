# Auto-Print

Aplikacja do zarządzania drukowaniem zdjęć na templatce A4.

## Uruchamianie z Docker

### Windows 11 / Linux / macOS

1. Zainstaluj Docker Desktop
2. Otwórz terminal w tym folderze
3. Uruchom kontener:

```bash
docker-compose up --build
```

4. Otwórz przeglądarkę: http://localhost:8080

### Zmiana portu

Edytuj plik `.env` i zmień wartość `PORT`:

```
PORT=9000
```

Następnie uruchom ponownie:

```bash
docker-compose down
docker-compose up --build
```

## Struktura folderów

- `incoming/` - nowe zdjęcia do przetworzenia
- `pending/` - zdjęcia zaakceptowane, oczekujące na druk
- `archive/` - wygenerowane PDF i zdjęcia
- `rejected/` - odrzucone pliki

## API

- `GET /` - Interfejs webowy
- `GET /api/state` - Aktualny stan aplikacji
- `POST /api/accept/{photo_id}` - Akceptuj zdjęcie
- `POST /api/reject/{photo_id}` - Odrzuć zdjęcie
- `POST /api/upload` - Prześlij zdjęcie

## Zatrzymywanie

```bash
docker-compose down
```
