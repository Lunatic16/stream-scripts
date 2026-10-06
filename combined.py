import requests
import re

def extract_stream_link():
    # Educational Example: Target URL jahan se data fetch karna hai
    target_url = "https://example.com/live-match"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    
    try:
        response = requests.get(target_url, headers=headers)
        if response.status_code == 200:
            # Regex ka use karke .m3u8 link dhundhna
            m3u8_links = re.findall(r'https?://[^\s]+\.m3u8', response.text)
            
            if m3u8_links:
                active_link = m3u8_links[0]
                print(f"Found active stream: {active_link}")
                
                # Link ko output file me save karna
                with open("stream.txt", "w") as f:
                    f.write(active_link)
            else:
                print("No .m3u8 link found on the page.")
        else:
            print(f"Failed to fetch page, status code: {response.status_code}")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    extract_stream_link()
