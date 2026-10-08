def parse_request(data):
    lines = data.decode(errors="ignore").split("\r\n")

    method, target, version = lines[0].split()

    return method, target, version