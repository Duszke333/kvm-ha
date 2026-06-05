from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def compute_mandelbrot(width=2000, height=2000, max_iter=500):
    pixels_computed = 0
    for x in range(width):
        for y in range(height):
            re = (x - width / 2.0) * 4.0 / width
            im = (y - height / 2.0) * 4.0 / width
            c = complex(re, im)
            z = 0

            for _ in range(max_iter):
                if abs(z) > 2:
                    break
                z = z * z + c
            pixels_computed += 1
    return pixels_computed


class LoadHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        pixels = compute_mandelbrot()

        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(f"generated {pixels} pixels\n".encode())


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8080), LoadHandler)
    print("serving on 8080...")
    server.serve_forever()


if __name__ == "__main__":
    main()
