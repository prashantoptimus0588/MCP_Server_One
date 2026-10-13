import urllib.request
url = "https://news.google.com/rss/search?q=Delhi&hl=en-IN&gl=IN&ceid=IN:en"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
print(urllib.request.urlopen(req, timeout=10).status)