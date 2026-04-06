class DNSCache:
    def __init__(self, size=3):
        self.cache = {}
        self.size = size

    def get(self, domain):
        return self.cache.get(domain)

    def add(self, domain, data):

        if len(self.cache) >= self.size:
            print("Cache full — flushing")
            self.cache.clear()

        self.cache[domain] = data