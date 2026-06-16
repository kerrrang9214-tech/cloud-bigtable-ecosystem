import logging
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part
from google.genai.types import Content, Part
from agent import create_adk_agent
from google.adk.auth.auth_credential import AuthCredential, AuthCredentialTypes, OAuth2Auth
import os
import uuid

# Global session service to maintain history within the same server process
SESSION_SERVICE = InMemorySessionService()

from google.adk.auth.credential_service.base_credential_service import BaseCredentialService

class ScopedCredentialService(BaseCredentialService):
    def __init__(self, access_token: str, refresh_token: str):
        super().__init__()
        self.access_token = access_token
        self.refresh_token = refresh_token

    async def load_credential(self, auth_config, callback_context):
        if not self.access_token:
            return None
            
        import time
        # Set expiry to 1 hour in the future to bypass ADK's refresher
        future_expiry = int(time.time()) + 3600
        
        oauth2_auth = OAuth2Auth(
            client_id=os.getenv("GOOGLE_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_at=future_expiry
        )
        return AuthCredential(
            auth_type=AuthCredentialTypes.OAUTH2,
            oauth2=oauth2_auth
        )

    async def save_credential(self, auth_config, callback_context):
        pass

from google.adk.memory.vertex_ai_memory_bank_service import VertexAiMemoryBankService

CONCIERGE_AGENT = None

async def chat_with_agent(user_email, message, access_token=None, refresh_token=None, session_id=None):
    """
    Handles a chat turn with the agent using the Runner.
    """
    global CONCIERGE_AGENT

    if not session_id:
        session_id = str(uuid.uuid4())

    try:
        existing_session = SESSION_SERVICE.get_session_sync(app_name="btagent", user_id=user_email, session_id=session_id)
    except Exception:
        existing_session = None

    if not existing_session:
       existing_session = SESSION_SERVICE.create_session_sync(app_name="btagent", user_id=user_email, session_id=session_id)

    credential_service = ScopedCredentialService(access_token, refresh_token)
    
    # Instantiate a fresh Memory Service bound to the current request event loop
    memory_service = VertexAiMemoryBankService(
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        agent_engine_id=os.getenv("VERTEX_AI_AGENT_ENGINE_ID", "local-demo-engine")
    )

    if CONCIERGE_AGENT is None:
        CONCIERGE_AGENT = create_adk_agent()

    # Initialize the Runner with the global session service
    runner = Runner(
        agent=CONCIERGE_AGENT, app_name="btagent",
        session_service=SESSION_SERVICE,
        memory_service=memory_service
    )
    
    # Prepare the message content with explicit role for Memory Bank parsing
    user_content = Content(role="user", parts=[Part(text=message)])
    # Compute the expected Key for OpenAPIToolset
    from google.adk.tools.openapi_tool.openapi_spec_parser.tool_auth_handler import ToolContextCredentialStore
    from google.adk.auth.auth_credential import AuthCredentialTypes, OAuth2Auth, AuthCredential
    import time
    
    if access_token:
        from sub_agents.agent_booking import calendar_toolset

        try:
            tools = await calendar_toolset.get_tools()
        except Exception:
            tools = []

        if tools:
            cal_tool = tools[0]
            auth_scheme = cal_tool._rest_api_tool.auth_scheme
            auth_credential = cal_tool._rest_api_tool.auth_credential
            
            store = ToolContextCredentialStore(None)
            key = store.get_credential_key(auth_scheme, auth_credential)
            
            
            future_expiry = int(time.time()) + 3600
            oauth2_auth = OAuth2Auth(
                client_id=os.getenv("GOOGLE_CLIENT_ID"),
                client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=future_expiry
            )
            # InMemorySessionService.get_session returns a deep copy.
            # We must mutate the actual session in the storage dictionary 
            # so that runner.run() picks up the injected credential.
            btagent_sessions = getattr(SESSION_SERVICE, "sessions", {}).get("btagent", {})
            user_sessions = btagent_sessions.get(user_email, {})
            real_session = user_sessions.get(session_id)
            
            if real_session:
                cred = AuthCredential(auth_type=AuthCredentialTypes.OAUTH2, oauth2=oauth2_auth)
                real_session.state[key] = cred.model_dump(mode="json")
            else:
                logging.warning(f"Session {session_id} not found for user {user_email} in InMemorySessionService. Credential injection skipped.")
    
    final_text = ""
    start_time = time.time()
    
    # Execute the agent asynchronously in the current request event loop
    events_stream = runner.run_async(
        user_id=user_email,
        session_id=session_id,
        new_message=user_content
    )
    
    async for event in events_stream:
        if event.is_final_response() and event.content:
            final_text = event.content.parts[0].text
            
    return final_text
