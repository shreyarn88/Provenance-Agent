import scrapy
import pymongo
from datetime import datetime, timezone

class BooksSpider(scrapy.Spider):
    name = "books"
    start_urls = ["https://books.toscrape.com/"]

    def __init__(self):
        self.client = pymongo.MongoClient("mongodb://localhost:27017/")
        self.db = self.client["thesis_db"]
        self.collection = self.db["books_raw"]
        self.records_collected = 0
        self.run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def parse(self, response):
        for book in response.css("article.product_pod"):
            item = {
                "title": book.css("h3 a::attr(title)").get(),
                "price": book.css(".price_color::text").get(),
                "rating": book.css("p.star-rating::attr(class)").get(),
                "url": response.url,
                "run_id": self.run_id,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
            self.collection.insert_one(item)
            self.records_collected += 1
            yield item

        # Follow pagination
        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield response.follow(next_page, self.parse)

    def closed(self, reason):
        # Emit real provenance event when spider finishes
        provenance_event = {
            "tool": "Scrapy",
            "event_type": "scraping_completed",
            "run_id": self.run_id,
            "spider_name": self.name,
            "source_url": self.start_urls[0],
            "output_dataset": "books_raw",
            "records_collected": self.records_collected,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "scrapy_spider_runner",
            "storage_location": "mongodb://localhost:27017/thesis_db/books_raw",
        }
        # Store provenance event in MongoDB alongside the data
        self.db["provenance_events"].insert_one(provenance_event)
        print(f"\n[Provenance] Spider finished. {self.records_collected} books scraped.")
        print(f"[Provenance] Run ID: {self.run_id}")
        print(f"[Provenance] Event saved to MongoDB provenance_events collection.")