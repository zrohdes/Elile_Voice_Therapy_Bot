import asyncio
import base64
import datetime
import os
import httpx
import streamlit as st
import threading
import requests
import re
from hume.client import AsyncHumeClient
from hume.empathic_voice.chat.socket_client import ChatConnectOptions
from hume.empathic_voice.chat.types import SubscribeEvent
from hume import MicrophoneInterface, Stream

# Disable SSL verification for corporate networks
os.environ['PYTHONHTTPSVERIFY'] = '0'
os.environ['CURL_CA_BUNDLE'] = ''

def translate_text(text, target_lang='ar'):
    """Simple translation using Google Translate API"""
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            'client': 'gtx',
            'sl': 'auto',
            'tl': target_lang,
            'dt': 't',
            'q': text
        }
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, params=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result and result[0]:
                return ''.join([item[0] for item in result[0] if item and item[0]]).strip()
    except:
        pass
    return "Translation unavailable"

def is_arabic(text):
    """Check if text contains Arabic characters"""
    arabic_pattern = re.compile(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]')
    arabic_chars = len(arabic_pattern.findall(text))
    total_chars = len([c for c in text if c.isalpha()])
    return total_chars > 0 and arabic_chars / total_chars > 0.3

class StreamlitWebSocketHandler:
    def __init__(self, input_language='auto'):
        self.byte_strs = Stream.new()
        self.chat_history = []
        self.is_connected = False
        self.input_language = input_language

    def set_input_language(self, language):
        self.input_language = language

    async def on_open(self):
        self.is_connected = True
        self.chat_history.append({
            "timestamp": datetime.datetime.now(tz=datetime.timezone.utc),
            "type": "system",
            "message": "Connection established. You can start speaking now."
        })

    async def on_message(self, message: SubscribeEvent):
        timestamp = datetime.datetime.now(tz=datetime.timezone.utc)
        
        if message.type == "user_message":
            emotions = self._extract_top_n_emotions(dict(message.models.prosody.scores), 3) if message.models.prosody else {}
            text = message.message.content.strip()
            
            # Determine language
            if self.input_language == 'auto':
                detected_is_arabic = is_arabic(text)
            else:
                detected_is_arabic = self.input_language == 'ar'
            
            # Create chat entry
            chat_entry = {
                "timestamp": timestamp,
                "type": "user",
                "message": text,
                "emotions": emotions,
                "original_language": "arabic" if detected_is_arabic else "english"
            }
            
            # Add translation
            if detected_is_arabic:
                chat_entry["english_translation"] = translate_text(text, 'en')
            else:
                chat_entry["arabic_translation"] = translate_text(text, 'ar')
            
            self.chat_history.append(chat_entry)
            
        elif message.type == "assistant_message":
            emotions = self._extract_top_n_emotions(dict(message.models.prosody.scores), 3) if message.models.prosody else {}
            self.chat_history.append({
                "timestamp": timestamp,
                "type": "assistant",
                "message": message.message.content,
                "emotions": emotions
            })
            
        elif message.type == "audio_output":
            await self.byte_strs.put(base64.b64decode(message.data.encode("utf-8")))
            
        elif message.type == "error":
            self.chat_history.append({
                "timestamp": timestamp,
                "type": "error",
                "message": f"Error ({message.code}): {message.message}"
            })
            raise RuntimeError(f"Received error message from Hume websocket ({message.code}): {message.message}")

    async def on_close(self):
        self.is_connected = False
        self.chat_history.append({
            "timestamp": datetime.datetime.now(tz=datetime.timezone.utc),
            "type": "system",
            "message": "Connection closed."
        })

    async def on_error(self, error):
        self.chat_history.append({
            "timestamp": datetime.datetime.now(tz=datetime.timezone.utc),
            "type": "error",
            "message": f"Error: {error}"
        })

    def _extract_top_n_emotions(self, emotion_scores: dict, n: int) -> dict:
        sorted_emotions = sorted(emotion_scores.items(), key=lambda item: item[1], reverse=True)
        return {emotion: score for emotion, score in sorted_emotions[:n]}

    def get_chat_history(self):
        return self.chat_history

async def run_voice_chat(handler, api_key, secret_key, config_id):
    custom_client = httpx.AsyncClient(verify=False, timeout=30.0)
    client = AsyncHumeClient(api_key=api_key, httpx_client=custom_client)
    options = ChatConnectOptions(config_id=config_id, secret_key=secret_key)

    try:
        async with client.empathic_voice.chat.connect_with_callbacks(
                options=options,
                on_open=handler.on_open,
                on_message=handler.on_message,
                on_close=handler.on_close,
                on_error=handler.on_error
        ) as socket:
            try:
                # Try to start microphone interface
                await asyncio.create_task(
                    MicrophoneInterface.start(socket, allow_user_interrupt=False, byte_stream=handler.byte_strs)
                )
            except Exception as audio_error:
                # Handle audio device errors gracefully
                error_msg = str(audio_error)
                if "querying device" in error_msg or "No Default Input Device Available" in error_msg:
                    handler.chat_history.append({
                        "timestamp": datetime.datetime.now(tz=datetime.timezone.utc),
                        "type": "error",
                        "message": "Audio device not available. This is expected when running on cloud servers. Please run locally for voice input functionality."
                    })
                else:
                    handler.chat_history.append({
                        "timestamp": datetime.datetime.now(tz=datetime.timezone.utc),
                        "type": "error",
                        "message": f"Audio error: {error_msg}"
                    })
                # Keep connection alive even without microphone
                await asyncio.sleep(1)  # Keep the connection open
                
    except Exception as e:
        handler.chat_history.append({
            "timestamp": datetime.datetime.now(tz=datetime.timezone.utc),
            "type": "error",
            "message": f"Connection error: {e}"
        })
    finally:
        await custom_client.aclose()

def run_async_chat(handler, api_key, secret_key, config_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_voice_chat(handler, api_key, secret_key, config_id))
    finally:
        loop.close()

def main():
    st.set_page_config(page_title="Hume AI Voice Chat", page_icon="🎙️", layout="wide")

    # Initialize session
    if 'session_initialized' not in st.session_state:
        if 'websocket_handler' in st.session_state:
            del st.session_state.websocket_handler
        if 'chat_thread' in st.session_state:
            del st.session_state.chat_thread
        if 'input_language' not in st.session_state:
            st.session_state.input_language = 'auto'
        st.session_state.session_initialized = True

    # Custom CSS
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap');
    
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
    
    .main-header {
        text-align: center;
        color: #1f2937;
        font-weight: 600;
        margin-bottom: 40px;
        font-size: 2.5rem;
    }
    
    .chat-container {
        padding: 24px;
        border-radius: 12px;
        margin: 16px 0;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
        border: 1px solid #e5e7eb;
    }
    
    .user-message {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        margin-left: 20%;
    }
    
    .assistant-message {
        background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        color: white;
        margin-right: 20%;
    }
    
    .system-message {
        background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
        color: white;
        text-align: center;
        margin: 8px 0;
        padding: 12px;
        font-size: 0.9rem;
    }
    
    .error-message {
        background: linear-gradient(135deg, #ff6b6b 0%, #ee5a24 100%);
        color: white;
        border: 1px solid #ff4757;
    }
    
    .timestamp {
        font-size: 0.75rem;
        opacity: 0.8;
        margin-bottom: 8px;
        font-weight: 300;
    }
    
    .message-content {
        font-size: 1rem;
        line-height: 1.6;
        margin-bottom: 12px;
        font-weight: 400;
        white-space: pre-line;
    }
    
    .arabic-text {
        direction: rtl;
        text-align: right;
    }
    
    .role-label {
        font-weight: 600;
        font-size: 0.9rem;
        opacity: 0.9;
        margin-bottom: 4px;
    }
    
    .status-indicator {
        display: inline-block;
        width: 12px;
        height: 12px;
        border-radius: 50%;
        margin-right: 8px;
    }
    
    .status-connected {
        background-color: #10b981;
        box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.2);
    }
    
    .status-disconnected {
        background-color: #ef4444;
        box-shadow: 0 0 0 2px rgba(239, 68, 68, 0.2);
    }
    </style>
    """, unsafe_allow_html=True)

    # Load environment variables (works both locally and on Streamlit Cloud)
    api_key = os.getenv("HUME_API_KEY") or st.secrets.get("HUME_API_KEY")
    secret_key = os.getenv("HUME_SECRET_KEY") or st.secrets.get("HUME_SECRET_KEY") 
    config_id = os.getenv("HUME_CONFIG_ID") or st.secrets.get("HUME_CONFIG_ID")
    
    # Check if credentials are available
    credentials_available = all([api_key, secret_key, config_id])
    
    if not credentials_available:
        st.error("⚠️ Missing Hume AI credentials. Please set up your environment variables:")
        st.code("""
        HUME_API_KEY=your_api_key_here
        HUME_SECRET_KEY=your_secret_key_here  
        HUME_CONFIG_ID=your_config_id_here
        """)
        st.info("For Streamlit Cloud deployment, add these as secrets in your app settings.")
        st.stop()
    
    # Check if running on Streamlit Cloud or in environment without audio devices
    try:
        import pyaudio
        # Test if audio devices are available
        audio = pyaudio.PyAudio()
        device_count = audio.get_device_count()
        audio.terminate()
        
        if device_count == 0:
            st.warning("🎤 **No audio devices detected**. This app works best with microphone access. Running in demo mode.")
            audio_available = False
        else:
            audio_available = True
    except Exception as e:
        st.warning("🎤 **Audio system not available**. This is normal on cloud deployments. For full voice functionality, run locally.")
        audio_available = False
    
    # Additional cloud detection
    is_cloud_deployment = (
        "streamlit.io" in (st.get_option("server.baseUrlPath") or "") or
        not audio_available or
        os.getenv("STREAMLIT_SHARING_MODE") == "1"
    )
    
    if is_cloud_deployment:
        st.info("💡 **Running in Cloud Mode**: Interface and translation features available. Clone and run locally for voice input: `streamlit run app.py`")

    # Header
    st.markdown("<h1 class='main-header'>Hume AI Voice Chat</h1>", unsafe_allow_html=True)

    # Controls
    col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
    
    with col1:
        if 'websocket_handler' in st.session_state and st.session_state.websocket_handler.is_connected:
            st.markdown("""
            <div style="display: flex; align-items: center; font-weight: 500; color: #059669;">
                <span class="status-indicator status-connected"></span>
                Connected - Voice chat active
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown("""
            <div style="display: flex; align-items: center; font-weight: 500; color: #dc2626;">
                <span class="status-indicator status-disconnected"></span>
                Disconnected
            </div>
            """, unsafe_allow_html=True)
    
    with col2:
        language_options = {'auto': 'Auto Detect', 'en': 'English', 'ar': 'Arabic'}
        selected_language = st.selectbox(
            "Input Language",
            options=list(language_options.keys()),
            format_func=lambda x: language_options[x],
            index=list(language_options.keys()).index(st.session_state.input_language),
            key="language_selector"
        )
        
        if selected_language != st.session_state.input_language:
            st.session_state.input_language = selected_language
            if 'websocket_handler' in st.session_state:
                st.session_state.websocket_handler.set_input_language(selected_language)
    
    with col3:
        if st.button("Start Voice Chat", disabled=not credentials_available, use_container_width=True):
            if 'websocket_handler' not in st.session_state:
                st.session_state.websocket_handler = StreamlitWebSocketHandler(st.session_state.input_language)
            else:
                st.session_state.websocket_handler.set_input_language(st.session_state.input_language)
            
            if 'chat_thread' not in st.session_state or not st.session_state.chat_thread.is_alive():
                st.session_state.chat_thread = threading.Thread(
                    target=run_async_chat,
                    args=(st.session_state.websocket_handler, api_key, secret_key, config_id)
                )
                st.session_state.chat_thread.daemon = True
                st.session_state.chat_thread.start()
                st.success("Voice chat started!")
                st.rerun()
    
    with col4:
        if st.button("New Chat", use_container_width=True):
            if 'websocket_handler' in st.session_state:
                st.session_state.websocket_handler.chat_history = []
            st.success("New conversation started!")
            st.rerun()

    # Chat display
    st.markdown("### Conversation")
    
    if 'websocket_handler' in st.session_state:
        chat_history = st.session_state.websocket_handler.get_chat_history()
        
        if chat_history:
            for entry in chat_history:
                timestamp_str = entry['timestamp'].strftime("%H:%M:%S")
                
                if entry['type'] == 'user':
                    css_class = "chat-container user-message"
                    role = "You"
                elif entry['type'] == 'assistant':
                    css_class = "chat-container assistant-message"
                    role = "AI Assistant"
                elif entry['type'] == 'system':
                    css_class = "chat-container system-message"
                    role = "System"
                elif entry['type'] == 'error':
                    css_class = "chat-container error-message"
                    role = "Error"
                else:
                    css_class = "chat-container"
                    role = entry['type'].title()
                
                # Build message content
                message_content = entry['message']
                
                # Add translation for user messages
                if entry['type'] == 'user':
                    if entry.get('original_language') == 'arabic' and 'english_translation' in entry:
                        message_content += f"\n\nEnglish: {entry['english_translation']}"
                    elif entry.get('original_language') == 'english' and 'arabic_translation' in entry:
                        message_content += f"\n\nArabic: {entry['arabic_translation']}"
                
                # Add emotions
                if 'emotions' in entry and entry['emotions']:
                    emotion_text = ' • '.join([f"{emotion.replace('_', ' ').title()}: {score:.2f}" for emotion, score in entry['emotions'].items()])
                    message_content += f"\n\nEmotions: {emotion_text}"
                
                # Apply Arabic text styling
                message_class = "message-content"
                if entry['type'] == 'user' and entry.get('original_language') == 'arabic':
                    message_class += " arabic-text"
                
                # Display message
                message_html = f"""
                <div class="{css_class}">
                    <div class="timestamp">{timestamp_str}</div>
                    <div class="role-label">{role}</div>
                    <div class="{message_class}">{message_content}</div>
                </div>
                """
                
                st.markdown(message_html, unsafe_allow_html=True)
        else:
            st.info("No conversation yet. Click 'Start Voice Chat' to begin speaking with the AI.")
    else:
        st.info("Click 'Start Voice Chat' to initialize the voice interface.")

    # Auto-refresh
    if 'websocket_handler' in st.session_state and st.session_state.websocket_handler.is_connected:
        import time
        time.sleep(2)
        st.rerun()

if __name__ == "__main__":
    main()
