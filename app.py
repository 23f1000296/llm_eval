import os
import asyncio
import json
import base64
import re
from datetime import datetime
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright
import anthropic
import httpx
import logging
from threading import Thread

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration - CHANGE THESE IN .env FILE
EMAIL = os.environ.get("STUDENT_EMAIL", "your@email.com")
SECRET = os.environ.get("STUDENT_SECRET", "your-secret-string")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Initialize Anthropic client
if ANTHROPIC_API_KEY:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
else:
    logger.warning("No Anthropic API key provided!")
    client = None

class QuizSolver:
    def __init__(self):
        self.start_time = None
        self.timeout_seconds = 170  # 2 min 50 sec (buffer before 3 min)
        
    def is_within_time_limit(self):
        """Check if we're still within the time limit"""
        if not self.start_time:
            return True
        elapsed = (datetime.now() - self.start_time).total_seconds()
        remaining = self.timeout_seconds - elapsed
        logger.info(f"Time remaining: {remaining:.1f} seconds")
        return elapsed < self.timeout_seconds
    
    async def fetch_quiz_page(self, url):
        """Fetch quiz page content using headless browser"""
        try:
            logger.info(f"Fetching quiz page: {url}")
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox']
                )
                context = await browser.new_context(
                    viewport={'width': 1920, 'height': 1080}
                )
                page = await context.new_page()
                
                # Navigate to URL
                await page.goto(url, wait_until='networkidle', timeout=30000)
                
                # Wait for content to render
                await page.wait_for_timeout(3000)
                
                # Get the full HTML content
                html_content = await page.content()
                
                # Get visible text content
                text_content = await page.evaluate('() => document.body.innerText')
                
                # Try to get decoded content if base64 encoded
                try:
                    decoded = await page.evaluate('''() => {
                        const resultDiv = document.querySelector('#result');
                        return resultDiv ? resultDiv.innerText : '';
                    }''')
                    if decoded:
                        text_content = decoded
                except:
                    pass
                
                await browser.close()
                
                logger.info(f"Successfully fetched page. Text length: {len(text_content)}")
                return {
                    'html': html_content,
                    'text': text_content,
                    'url': url
                }
        except Exception as e:
            logger.error(f"Error fetching page: {e}")
            return None
    
    async def download_file(self, file_url):
        """Download a file from URL"""
        try:
            logger.info(f"Downloading file: {file_url}")
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
                response = await http_client.get(file_url)
                response.raise_for_status()
                logger.info(f"Downloaded {len(response.content)} bytes")
                return response.content
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            return None
    
    async def solve_with_claude(self, page_data):
        """Use Claude to interpret and solve the quiz"""
        try:
            if not client:
                logger.error("Anthropic client not initialized")
                return None
                
            logger.info("Asking Claude to interpret the quiz...")
            
            prompt = f"""You are helping solve a data analysis quiz. Analyze this quiz page and extract key information.

URL: {page_data['url']}

PAGE CONTENT:
{page_data['text']}

Your task:
1. Identify the EXACT question being asked
2. Find ALL URLs for data files that need to be downloaded (PDFs, CSVs, Excel files, etc.)
3. Find the submit URL where the answer should be posted
4. Determine what analysis/calculations are needed
5. Determine the expected answer format (number, string, boolean, array, or JSON object)

IMPORTANT: Look for ALL links in the page, including those in <a href="..."> tags.

Respond with ONLY a JSON object (no markdown, no explanation):
{{
    "question": "exact question text",
    "data_urls": ["list of all data file URLs found"],
    "submit_url": "the endpoint where answer should be posted",
    "analysis_needed": "what needs to be calculated/analyzed",
    "answer_format": "number|string|boolean|array|json",
    "key_details": "any other important details"
}}"""

            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}]
            )
            
            response_text = message.content[0].text.strip()
            logger.info(f"Claude's interpretation: {response_text[:500]}...")
            
            # Extract JSON from response
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                logger.info(f"Parsed quiz info: {json.dumps(parsed, indent=2)}")
                return parsed
            else:
                logger.error("Could not find JSON in Claude's response")
                return None
                
        except Exception as e:
            logger.error(f"Error solving with Claude: {e}")
            return None
    
    async def analyze_data_with_claude(self, quiz_info, data_content=None, page_text=None):
        """Use Claude to analyze data and provide the answer"""
        try:
            if not client:
                logger.error("Anthropic client not initialized")
                return None
                
            logger.info("Asking Claude to analyze and solve...")
            
            # Prepare data description
            data_info = ""
            if data_content:
                if isinstance(data_content, bytes):
                    # Show first part of file as base64
                    sample = base64.b64encode(data_content[:500]).decode()
                    data_info = f"File data (first 500 bytes as base64): {sample}\nTotal size: {len(data_content)} bytes"
                else:
                    data_info = f"Data: {str(data_content)[:2000]}"
            elif page_text:
                data_info = f"Page text: {page_text[:2000]}"
            
            prompt = f"""Solve this data analysis quiz and provide the EXACT answer.

QUESTION: {quiz_info['question']}

ANALYSIS NEEDED: {quiz_info['analysis_needed']}

EXPECTED FORMAT: {quiz_info['answer_format']}

DATA:
{data_info}

KEY DETAILS: {quiz_info.get('key_details', 'None')}

Instructions:
1. Perform the required analysis/calculation
2. Provide ONLY the answer in the exact format specified
3. If format is "number": respond with just the number (e.g., 12345)
4. If format is "string": respond with just the text (e.g., "hello")
5. If format is "boolean": respond with true or false
6. If format is "json": respond with valid JSON object
7. Do NOT include explanations, just the answer

YOUR ANSWER:"""

            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}]
            )
            
            answer_text = message.content[0].text.strip()
            logger.info(f"Claude's raw answer: {answer_text}")
            
            # Parse answer based on expected format
            answer = self.parse_answer(answer_text, quiz_info['answer_format'])
            logger.info(f"Parsed answer: {answer} (type: {type(answer).__name__})")
            
            return answer
            
        except Exception as e:
            logger.error(f"Error analyzing data: {e}")
            return None
    
    def parse_answer(self, answer_text, answer_format):
        """Parse answer based on expected format"""
        try:
            answer_text = answer_text.strip()
            
            if answer_format == 'number':
                # Extract number from response
                num_match = re.search(r'-?\d+\.?\d*', answer_text)
                if num_match:
                    num_str = num_match.group()
                    return float(num_str) if '.' in num_str else int(num_str)
                return 0
                
            elif answer_format == 'boolean':
                lower = answer_text.lower()
                return lower in ['true', 'yes', '1', 'correct']
                
            elif answer_format == 'json':
                # Try to parse as JSON
                json_match = re.search(r'\{.*\}', answer_text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
                return json.loads(answer_text)
                
            elif answer_format == 'array':
                # Try to parse as array
                arr_match = re.search(r'\[.*\]', answer_text, re.DOTALL)
                if arr_match:
                    return json.loads(arr_match.group())
                return json.loads(answer_text)
            else:
                # String - return as is, but remove quotes if present
                if answer_text.startswith('"') and answer_text.endswith('"'):
                    return answer_text[1:-1]
                return answer_text
                
        except Exception as e:
            logger.error(f"Error parsing answer: {e}")
            return answer_text
    
    async def submit_answer(self, submit_url, quiz_url, answer):
        """Submit the answer to the quiz endpoint"""
        try:
            payload = {
                "email": EMAIL,
                "secret": SECRET,
                "url": quiz_url,
                "answer": answer
            }
            
            logger.info(f"Submitting to {submit_url}")
            logger.info(f"Payload: {json.dumps(payload, indent=2)}")
            
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                response = await http_client.post(submit_url, json=payload)
                response.raise_for_status()
                result = response.json()
                
                logger.info(f"Submit result: {json.dumps(result, indent=2)}")
                return result
                
        except Exception as e:
            logger.error(f"Error submitting answer: {e}")
            return {"correct": False, "reason": str(e)}
    
    async def solve_quiz_chain(self, initial_url):
        """Solve a chain of quiz questions"""
        self.start_time = datetime.now()
        current_url = initial_url
        attempts = 0
        max_attempts = 20
        
        logger.info("=" * 80)
        logger.info(f"Starting quiz chain from: {initial_url}")
        logger.info("=" * 80)
        
        while current_url and self.is_within_time_limit() and attempts < max_attempts:
            attempts += 1
            logger.info(f"\n{'='*80}")
            logger.info(f"ATTEMPT {attempts}: {current_url}")
            logger.info(f"{'='*80}\n")
            
            # Fetch the quiz page
            page_data = await self.fetch_quiz_page(current_url)
            if not page_data:
                logger.error("❌ Failed to fetch page")
                break
            
            # Use Claude to understand the quiz
            quiz_info = await self.solve_with_claude(page_data)
            if not quiz_info:
                logger.error("❌ Failed to parse quiz")
                break
            
            # Download any required data files
            data_content = None
            if quiz_info.get('data_urls'):
                logger.info(f"Found {len(quiz_info['data_urls'])} data URLs")
                for data_url in quiz_info['data_urls']:
                    if data_url and data_url.startswith('http'):
                        data_content = await self.download_file(data_url)
                        if data_content:
                            break
            
            # Analyze and get answer
            answer = await self.analyze_data_with_claude(
                quiz_info, 
                data_content=data_content,
                page_text=page_data['text']
            )
            
            if answer is None:
                logger.error("❌ Failed to generate answer")
                break
            
            # Submit answer
            submit_url = quiz_info.get('submit_url')
            if not submit_url:
                logger.error("❌ No submit URL found")
                break
            
            result = await self.submit_answer(submit_url, current_url, answer)
            
            if result.get('correct'):
                logger.info(f"✅ CORRECT! Answer was: {answer}")
                current_url = result.get('url')
                if not current_url:
                    logger.info("🎉 Quiz chain completed successfully!")
                    break
            else:
                reason = result.get('reason', 'Unknown reason')
                logger.warning(f"❌ INCORRECT: {reason}")
                logger.warning(f"   Our answer was: {answer}")
                
                # Check if we can proceed to next quiz or should retry
                next_url = result.get('url')
                if next_url and next_url != current_url:
                    logger.info(f"Moving to next quiz: {next_url}")
                    current_url = next_url
                else:
                    # Could implement retry logic here
                    logger.info("No new URL provided, stopping.")
                    break
            
            # Small delay between attempts
            await asyncio.sleep(1)
        
        elapsed = (datetime.now() - self.start_time).total_seconds()
        logger.info(f"\n{'='*80}")
        logger.info(f"Quiz solving completed after {attempts} attempts in {elapsed:.1f} seconds")
        logger.info(f"{'='*80}\n")

def run_async_solver(url):
    """Helper to run async solver in a thread"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        solver = QuizSolver()
        loop.run_until_complete(solver.solve_quiz_chain(url))
        loop.close()
    except Exception as e:
        logger.error(f"Error in async solver: {e}")

@app.route('/quiz', methods=['POST'])
def handle_quiz():
    """Main endpoint to receive quiz tasks"""
    try:
        data = request.get_json()
        
        if not data:
            logger.warning("Received request with no JSON data")
            return jsonify({'error': 'Invalid JSON'}), 400
        
        logger.info(f"Received request: {json.dumps(data, indent=2)}")
        
        # Verify secret
        if data.get('secret') != SECRET:
            logger.warning(f"Invalid secret: {data.get('secret')}")
            return jsonify({'error': 'Invalid secret'}), 403
        
        # Verify email
        if data.get('email') != EMAIL:
            logger.warning(f"Invalid email: {data.get('email')}")
            return jsonify({'error': 'Invalid email'}), 403
        
        quiz_url = data.get('url')
        if not quiz_url:
            logger.warning("No URL provided in request")
            return jsonify({'error': 'No URL provided'}), 400
        
        logger.info(f"✅ Valid request for quiz: {quiz_url}")
        
        # Start solving in background thread
        thread = Thread(target=run_async_solver, args=(quiz_url,))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'status': 'started',
            'message': 'Quiz solving initiated',
            'url': quiz_url
        }), 200
        
    except Exception as e:
        logger.error(f"Error handling request: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'api_key_configured': bool(ANTHROPIC_API_KEY),
        'email_configured': EMAIL != 'your@email.com'
    }), 200

@app.route('/', methods=['GET'])
def home():
    """Home endpoint with instructions"""
    return jsonify({
        'service': 'LLM Quiz Solver',
        'status': 'running',
        'endpoints': {
            'POST /quiz': 'Submit quiz URL for solving',
            'GET /health': 'Health check',
            'GET /': 'This page'
        },
        'configuration': {
            'email': EMAIL,
            'secret_configured': bool(SECRET and SECRET != 'your-secret-string'),
            'api_key_configured': bool(ANTHROPIC_API_KEY)
        },
        'ready': bool(ANTHROPIC_API_KEY and EMAIL != 'your@email.com')
    }), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Starting server on port {port}")
    logger.info(f"Email: {EMAIL}")
    logger.info(f"Secret configured: {bool(SECRET and SECRET != 'your-secret-string')}")
    logger.info(f"API key configured: {bool(ANTHROPIC_API_KEY)}")
    app.run(host='0.0.0.0', port=port, debug=False)
