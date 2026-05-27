param(
    [string]$BaseUrl = "https://10.4.51.232:18443"
)

$ErrorActionPreference = "Stop"

curl.exe -k -fsS "$BaseUrl/health"
curl.exe -k -fsS "$BaseUrl/api/search?q=%CE%95%CF%86%CE%B1%CF%81%CE%BC%CE%BF%CE%B3%CE%AE%20%CE%94%CE%9D%CE%A5&size=3"
