def parse_request(data):
    lines = data.decode(errors="ignore").split("\r\n")

    method, target, version = lines[0].split()

    headers = {}

    for line in lines[1:]:
        if not line:
            break

        key, value = line.split(":", 1)
        headers[key.lower()] = value.strip()

    return method, target, version, headers

    