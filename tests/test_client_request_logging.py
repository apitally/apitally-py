import gzip
import json


def test_request_logger():
    from apitally.client.request_logging import RequestDict, RequestLogger, RequestLoggingConfig, ResponseDict

    config = RequestLoggingConfig(enabled=True)
    request_logger = RequestLogger(config=config)
    assert request_logger.enabled

    request: RequestDict = {
        "method": "GET",
        "path": "/test",
        "url": "http://localhost:8000/test?foo=bar",
        "headers": {"Accept": "application/json"},
    }
    response: ResponseDict = {
        "status_code": 200,
        "response_time": 0.1,
        "headers": {"Content-Type": "application/json"},
        "size": 100,
    }
    for _ in range(10):
        request_logger.log_request(request, response)

    request_logger.write_to_file()
    assert request_logger.current_file_size > 0

    request_logger.rotate_file()
    file = request_logger.get_file()
    assert file is not None

    compressed_data1 = b""
    for chunk in file.stream_lines_compressed():
        compressed_data1 += chunk
    assert len(compressed_data1) > 0

    with file.open_compressed() as fp:
        compressed_data2 = fp.read()
    assert compressed_data1 == compressed_data2

    file.delete()

    decompressed_data = gzip.decompress(compressed_data1)
    json_lines = decompressed_data.decode("utf-8").strip().split("\n")
    assert len(json_lines) == 10
    for json_line in json_lines:
        item = json.loads(json_line)
        assert item["request"] == request
        assert item["response"] == response

    request_logger.close()
