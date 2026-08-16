import csv
import datetime
import logging
import json
import os
import time
from urllib.parse import urlencode, urlparse, urlunparse
from typing import Any
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError, Page, Browser, Playwright
from playwright_stealth import stealth_sync

# Адреса API
BASE_URL = "https://www.avito.ru"
API_ENDPOINT = "/web/1/js/items"

# Параметры поиска для API Avito
SEARCH_PARAMS = {
    "params[110056]": "418153",   # Состояние: только новое
    "s": "1",                     # Состояние: только новое
    "params[167128]": "3270783",  # Сортировка: по возрастанию цены
    "sortDefault": "1",           # Сортировка: по возрастанию цены
    "categoryId": "10",           # Категория: Главная/Транспорт/Запчасти и аксессуары
    "locationId": "107620",       # Регион: Москва и Московская область
    "localPriority": "1",         # Cначала из выбранного региона
}

# Количество объявлений для сохранения
MAX_ITEMS_PER_ARTICLE = 5

# Имя выходного файла
OUTPUT_CSV_FILE = "result.csv"

# Имя файла с поисковыми запросами
QUERIES_FILE = "queries.txt"

# Воспомогательные файлы и тестовые данные
LOCATIONS_FILE = "slocations.json"
TEST_FILES_DIR = "testfiles"

# Заголовки для CSV
CSV_HEADERS = [
    "искомый артикул", "поисковый запрос", "заголовок", "цена",
    "город или регион", "состояние товара", "ссылка на объявление",
    "место по цене", "дата и время проверки"
]

# Настройки запуска браузера
LAUNCH_OPTIONS = {
    "headless": True,
}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Исключение собственных объявлений
HNX_PROFILE_TITLE = "HNX AUTO"
HNX_PROFILE_LINK_PART = "brands/7e3fce5aeafb47fd059f11dda1008e4c"

# --- Logging Setup ---
logger = logging.getLogger('AvitoParser')
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(ch)


class AvitoParser:
    def __init__(self):
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.articles: list[str] = self._read_queries()
        self.location_ids: set[int] = self._read_locations()
        self.use_test_files = False
        self.results: list[dict[str, Any]] = []

    def _read_queries(self) -> list[str]:
        try:
            with open(QUERIES_FILE, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            logger.error(f"Файл с артикулами '{QUERIES_FILE}' не найден.")
            return []

    def _read_locations(self) -> set[int]:
        try:
            with open(LOCATIONS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                location_ids = set()
                for loc in data.get('result', {}).get('locations', []):
                    if 'id' in loc:
                        location_ids.add(loc['id'])
                    if 'parent' in loc and isinstance(loc.get('parent'), dict) and 'id' in loc['parent']:
                        location_ids.add(loc['parent']['id'])
                return location_ids
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Ошибка чтения или парсинга файла локаций '{LOCATIONS_FILE}': {e}")
            return set()

    def _init_browser(self):
        logger.info("Инициализация браузера...")
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(**LAUNCH_OPTIONS)
            context = self.browser.new_context(user_agent=USER_AGENT)
            self.page = context.new_page()
            stealth_sync(self.page)
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                logger.critical(
                    "Не найден исполняемый файл браузера Chromium. "
                    "Убедитесь, что браузеры для Playwright установлены. "
                    "Выполните в терминале команду: playwright install chromium"
                )
            raise e

    def _check_access(self):
        logger.info("Переход на главную страницу Avito для инициализации сессии.")
        try:
            self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=10000)
            self._save_page()
            if "доступ ограничен" in self.page.title().lower():
                logger.warning("Доступ к Avito ограничен. Переключение на использование тестовых файлов.")
                self.use_test_files = True
        except TimeoutError:
            self._save_page()
            logger.error("Не удалось загрузить главную страницу Avito. Переключение на использование тестовых файлов.")
            self.use_test_files = True
        except Exception as e:
            logger.error(f"Произошла ошибка при доступе к Avito: {e}. Переключение на использование тестовых файлов.")
            self.use_test_files = True

    def _fetch_data(self, article: str, search_url: str) -> dict[str, Any] | None:
        if self.use_test_files:
            logger.info(f"Загрузка данных из тестового файла для артикула '{article}'")
            test_file_path = os.path.join(TEST_FILES_DIR, f"{article}.json")
            try:
                with open(test_file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as e:
                logger.error(f"Ошибка чтения тестового файла '{test_file_path}': {e}")
                return None
        else:
            logger.info(f"Выполнение запроса к API для артикула '{article}'")
            try:
                self.page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                # Playwright возвращает HTML-обертку, извлекаем JSON из pre-тега
                json_text = self.page.locator("pre").inner_text()
                return json.loads(json_text)
            except (TimeoutError, json.JSONDecodeError) as e:
                self._save_page()
                logger.error(f"Ошибка при запросе или парсинге JSON для '{article}': {e}")
                return None
            except Exception as e:
                logger.error(f"Неожиданная ошибка при запросе для '{article}': {e}")
                return None

    def _is_valid_item(self, item: dict[str, Any], article: str) -> bool:
        try:
            # 1. Артикул в названии или параметрах
            title = item.get('title', '').lower()
            spare_parts: list[str] = []
            iva_payload = item.get('iva', {}).get('SparePartsParamsStep', {})
            for user_info_payload in iva_payload:
                spare_parts_payload = item.get('payload', {})
                if spare_parts_payload and spare_parts_payload.get('text', ''):
                    spare_parts.append(spare_parts_payload.get('text', '').lower())
            if article.lower() not in title and article.lower() not in ",".join(spare_parts):
                return False

            # 2. Валидная локация
            if item.get('locationId') not in self.location_ids:
                return False

            # 3. Цена
            price_detailed = item.get('priceDetailed', {})
            if not price_detailed.get('hasValue') or not price_detailed.get('value', 0) > 0:
                return False

            # 4. Исключение HNX
            for user_info_payload in iva_payload:
                user_info = user_info_payload.get('payload', {}).get('profile', {}) if user_info_payload else {}
                if HNX_PROFILE_TITLE.lower() in user_info.get('title', '').lower():
                    return False
                if HNX_PROFILE_LINK_PART.lower() in user_info.get('link', '').lower():
                    return False

            return True
        except Exception as e:
            logger.warning(f"Ошибка при валидации объявления: {e}. Объявление будет пропущено.")
            return False

    def _parse_data(self, data: dict[str, Any], article: str):
        if not data or 'catalog' not in data:
            logger.warning(f"Нет 'catalog' в ответе для артикула '{article}'")
            return
        catalog = data['catalog']
        if not catalog or 'items' not in catalog:
            logger.warning(f"Нет 'items' в ответе для артикула '{article}'")
            return

        search_url = BASE_URL + data.get("url", "")
        valid_items = []
        for item in catalog['items']:
            if item.get('type') == 'item' and self._is_valid_item(item, article):
                try:
                    location = "Не указан"
                    if item.get("geo", {}).get("geoReferences") and item["geo"]["geoReferences"]:
                        location = item["geo"]["geoReferences"][0].get("content", "Не указан")
                    elif item.get("geo", {}).get("formattedAddress"):
                        location = item["geo"].get("formattedAddress", "Не указан")
                    state = "Б/У"
                    iva_payload = item.get('iva', {}).get('BadgeBarStep', {})
                    for badge_info_payload in iva_payload:
                        badges_payload = badge_info_payload.get('payload', {}).get('badges', {})
                        for badge in badges_payload:
                            if badge.get('id', None) == 2969:
                                state = "Новое"
                                break
                    

                    parsed_item = {
                        "искомый артикул": article,
                        "поисковый запрос": search_url,
                        "заголовок": item.get("title", "Без заголовка"),
                        "цена": int(item["priceDetailed"]["value"]),
                        "город или регион": location,
                        "состояние товара": state,
                        "ссылка на объявление": urlunparse(urlparse(BASE_URL + item.get("urlPath", ""))._replace(query="", fragment="")),  # Обрезать лишние параметры из ссылки
                        "дата и время проверки": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    valid_items.append(parsed_item)
                except (KeyError, ValueError, TypeError) as e:
                    logger.error(f"Ошибка извлечения данных из объявления для артикула '{article}': {e}")

        if not valid_items:
            self.results.append({
                "искомый артикул": article, "поисковый запрос": search_url, "заголовок": "не найдено",
                "цена": "", "город или регион": "", "состояние товара": "", "ссылка на объявление": "",
                "место по цене": "", "дата и время проверки": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            return

        # Сортировка и добавление места по цене
        valid_items.sort(key=lambda x: x["цена"])
        for i, res in enumerate(valid_items[:MAX_ITEMS_PER_ARTICLE]):
            res["место по цене"] = i + 1
            self.results.append(res)

    def _write_results(self):
        if not self.results:
            logger.warning("Нет данных для записи в CSV.")
            return

        logger.info(f"Запись {len(self.results)} строк в {OUTPUT_CSV_FILE}")
        try:
            with open(OUTPUT_CSV_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, quoting=csv.QUOTE_ALL)
                writer.writeheader()
                writer.writerows(self.results)
            logger.info("Результаты успешно сохранены.")
        except OSError as e:
            logger.error(f"Не удалось записать результаты в файл: {e}")

    def _save_page(self, name: str = ""):
        if name and not name.startswith("_"):
            name = f"_{name}"
        screenshot_path = Path(".", "debug", f"page_{time.time()}{name}.png")
        html_path = Path(".", "debug", f"page_{time.time()}{name}.html")
        self.page.screenshot(path=screenshot_path)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(self.page.content())

    def run(self):
        self._init_browser()
        self._check_access()

        for article in self.articles:
            logger.info(f"--- Обработка артикула: {article} ---")
            api_params = {"name": article, **SEARCH_PARAMS}
            search_url = f"{BASE_URL}{API_ENDPOINT}?{urlencode(api_params)}"

            data = self._fetch_data(article, search_url)

            if data:
                self._parse_data(data, article)
            else:
                logger.error(f"Не удалось получить данные для артикула '{article}'")
                self.results.append({
                    "искомый артикул": article, "поисковый запрос": "", "заголовок": "ошибка",
                    "цена": "", "город или регион": "", "состояние товара": "", "ссылка на объявление": "",
                    "место по цене": "", "дата и время проверки": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })

        self._write_results()

    def close(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
        logger.info("Браузер закрыт.")


def main():
    parser = AvitoParser()
    try:
        parser.run()
    except Exception as e:
        logger.critical(f"Произошла непредвиденная ошибка: {e}", exc_info=True)
    finally:
        parser.close()


if __name__ == "__main__":
    main()
