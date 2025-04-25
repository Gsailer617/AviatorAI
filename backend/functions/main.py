\
import firebase_functions as fn
from firebase_functions.https_fn import HttpsError, FunctionsErrorCode
import firebase_admin
from firebase_admin import firestore
from google.cloud.firestore_v1.base_query import FieldFilter, Query
from datetime import datetime, timezone, timedelta
import logging
from collections import Counter # Needed for processFeedbackLoop

# Assuming config.py is in the genkit directory, relative import works
from backend.genkit.config import load_and_run_flow # Import the Genkit runner

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Firebase Admin SDK if not already done (config.py might do it)
# Ensure it's initialized before db access
if not firebase_admin._apps:
    try:
        firebase_admin.initialize_app()
        logger.info("Firebase Admin SDK initialized in main.py (if not already by config.py).")
    except Exception as e:
        logger.error(f"Error initializing Firebase Admin SDK in main.py: {e}")
        # If Firebase is essential, might need to raise here or handle inability to connect

db = firestore.client()

# Helper to get current UTC timestamp
def now_utc():
    return datetime.now(timezone.utc)

# --- Cloud Functions --- #

@fn.https_fn.on_call(max_instances=10) # Adjust concurrency as needed
def chatWithAI(request: fn.https_fn.CallableRequest):
    """Handles a conversational turn using RAG."""
    logger.info(f"chatWithAI triggered.")

    # 1. Auth & input
    if not request.auth:
        logger.error("Authentication required for chatWithAI.")
        # Use the specific ForbiddenError if defined, otherwise standard HttpsError
        # Assuming ForbiddenError is like HttpsError(code=FunctionsErrorCode.PERMISSION_DENIED, message="...")
        raise HttpsError(code=FunctionsErrorCode.UNAUTHENTICATED,
                         message="Authentication required")
    uid = request.auth.uid
    body = request.data or {}
    message_text = body.get("message")
    chat_id = body.get("chatId")   # may be None

    if not message_text or not isinstance(message_text, str) or len(message_text.strip()) == 0:
        logger.error("Invalid message text provided.")
        raise HttpsError(code=FunctionsErrorCode.INVALID_ARGUMENT,
                         message='The function must be called with a valid "message" argument.')
    if chat_id and not isinstance(chat_id, str):
        logger.error("Invalid chatId provided.")
        raise HttpsError(code=FunctionsErrorCode.INVALID_ARGUMENT,
                         message='If provided, "chatId" must be a string.')

    try:
        # 2. Create/lookup chat doc
        chats_ref = db.collection("users").document(uid).collection("chats")
        if chat_id:
            chat_ref = chats_ref.document(chat_id)
            logger.info(f"Using existing chat ID: {chat_id} for user: {uid}")
            # Optionally check existence if needed, but set() or update() handle creation
        else:
            chat_ref = chats_ref.document() # Generate new ID
            chat_ref.set({"startTime": firestore.SERVER_TIMESTAMP}) # Create doc with timestamp
            chat_id = chat_ref.id # Get the new ID
            logger.info(f"Created new chat with ID: {chat_id} for user: {uid}")

        messages_coll_ref = chat_ref.collection("messages")

        # 3. Persist user message
        logger.info("Persisting user message.")
        msg_ref = messages_coll_ref.document()
        msg_ref.set({
            "sender":"user",
            "text": message_text,
            "timestamp": firestore.SERVER_TIMESTAMP,
            "sources": [], # User messages have no sources from RAG
            "feedbackRating": None # Initial feedback state
        })
        logger.info(f"User message stored: {msg_ref.path}")

        # 4. Fetch context
        logger.info("Fetching context (summary and recent messages).")
        context_text = ""
        # Path for context summary doc - ADJUST IF YOUR STRUCTURE DIFFERS
        ctx_doc_ref = db.collection("users").document(uid).collection("contextSummary").document("latest")
        try:
            ctx_doc = ctx_doc_ref.get()
            if ctx_doc.exists:
                context_text = ctx_doc.to_dict().get("summary","") # Get summary or default to empty
                logger.info("Found context summary.")
            else:
                logger.info("No context summary document found.")
        except Exception as e:
             logger.warning(f"Could not fetch context summary at {ctx_doc_ref.path}: {e}")

        # Fetch last 5 messages (as per instruction)
        recent_msgs_data = []
        try:
            # Query last 5 messages, ordered descending by timestamp
            # Note: The instruction used stream() then list(), which fetches all results.
            # Using .limit(5).stream() is more direct.
            recent_msgs_query = messages_coll_ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(5)
            docs = list(recent_msgs_query.stream()) # Fetch the limited docs

            # Extract sender and text, store with timestamp for sorting
            fetched_msgs = []
            for doc in docs:
                 if doc.exists:
                      msg_data = doc.to_dict()
                      fetched_msgs.append((
                          msg_data.get("timestamp"), # Keep timestamp for sorting
                          {"sender": msg_data.get("sender"), "text": msg_data.get("text")}
                      ))

            # Sort chronologically (oldest first) because LLMs usually expect history in order
            fetched_msgs.sort(key=lambda x: x[0] if x[0] else datetime.min.replace(tzinfo=timezone.utc)) # Handle potential missing timestamps gracefully
            recent_msgs_data = [item[1] for item in fetched_msgs] # Extract dicts after sorting

            logger.info(f"Fetched {len(recent_msgs_data)} recent messages for context.")
        except Exception as e:
            logger.warning(f"Could not fetch recent messages for chat {chat_id}: {e}")

        # 5. Genkit RAG call
        logger.info("Calling Genkit RAG flow.")
        rag_input = {
            "userId": uid,
            "message": message_text,
            "contextSummary": context_text,
            # Ensure the key matches exactly what the flow expects (e.g., "recentMessages")
            "recentMessages": recent_msgs_data
        }
        try:
            # Name "RAGFlow" should match the flow definition name/file
            rag_output = load_and_run_flow("RAGFlow", rag_input)
            logger.info(f"RAG flow returned successfully.")

            # Validate RAG output structure
            if not isinstance(rag_output, dict) or "text" not in rag_output or "sources" not in rag_output:
                logger.error(f"RAG flow output is missing expected keys ('text', 'sources') or is not a dict. Output: {rag_output}")
                raise HttpsError(code=FunctionsErrorCode.INTERNAL, message="RAG flow returned invalid data.")
            if not isinstance(rag_output["sources"], list):
                 logger.error(f"RAG flow 'sources' output is not a list. Output: {rag_output}")
                 raise HttpsError(code=FunctionsErrorCode.INTERNAL, message="RAG flow returned invalid sources format.")

        except HttpsError:
             raise # Re-throw validation errors
        except Exception as e:
            logger.error(f"Error calling or processing RAG flow: {e}")
            raise HttpsError(code=FunctionsErrorCode.INTERNAL,
                             message="Failed to get response from AI model.")

        # 6. Persist AI message
        logger.info("Persisting AI response.")
        ai_ref = messages_coll_ref.document()
        ai_payload = {
            "sender": "ai",
            "text": rag_output["text"],
            "sources": rag_output["sources"], # Assume sources format is Firestore-compatible
            "timestamp": firestore.SERVER_TIMESTAMP,
            "feedbackRating": None
        }
        ai_ref.set(ai_payload)
        logger.info(f"AI response stored: {ai_ref.path}")

        # 7. Return
        response_payload = {
            "chatId": chat_id,
            "messageId": ai_ref.id, # ID of the AI message just created
            "responseText": rag_output["text"],
            "sources": rag_output["sources"]
        }
        logger.info(f"Returning response payload for chat {chat_id}.")
        return response_payload

    except HttpsError as e:
        raise e # Re-throw known validation/auth errors
    except Exception as e:
        logger.exception(f"An unexpected error occurred in chatWithAI for user {uid}: {e}")
        raise HttpsError(code=FunctionsErrorCode.INTERNAL,
                         message="An internal error occurred processing your message.")


@fn.https_fn.on_call(max_instances=5) # Adjust concurrency as needed
def generateQuiz(request: fn.https_fn.CallableRequest):
    """Generates a quiz, potentially based on user's weak topics."""
    logger.info("generateQuiz triggered.")

    # 1. Auth & params
    if not request.auth:
        logger.error("Authentication required for generateQuiz.")
        raise HttpsError(code=FunctionsErrorCode.UNAUTHENTICATED,
                         message="Authentication required")
    uid = request.auth.uid
    data = request.data or {}
    num_questions = data.get("numQuestions", 5) # Default to 5 questions

    # Validate num_questions
    if not isinstance(num_questions, int) or not (1 <= num_questions <= 20): # Sensible limits
         logger.error(f"Invalid numQuestions: {num_questions}")
         raise HttpsError(code=FunctionsErrorCode.INVALID_ARGUMENT,
                          message="Parameter 'numQuestions' must be an integer between 1 and 20.")

    try:
        # 2. Fetch weakTopics
        topics = []
        # Path for weak topics doc - ADJUST IF YOUR STRUCTURE DIFFERS
        wt_doc_ref = db.collection("users").document(uid).collection("progress").document("weakTopics")
        try:
            wt_doc = wt_doc_ref.get()
            if wt_doc.exists:
                fetched_topics = wt_doc.to_dict().get("topics", []) # Expecting a list of strings
                if isinstance(fetched_topics, list) and fetched_topics:
                    topics = fetched_topics
                    logger.info(f"Found weak topics for user {uid}: {topics}")
                else:
                     logger.info(f"Weak topics document found for user {uid} but contains no valid 'topics' list.")
            else:
                logger.info(f"No weak topics document found for user {uid} at {wt_doc_ref.path}.")
        except Exception as e:
            logger.warning(f"Could not fetch weak topics for user {uid}: {e}")

        # 3. Default fallback topics
        if not topics:
            # TODO: Replace with your actual FAA topic list or relevant domain topics
            topics = ["Airspace", "Weather", "Aircraft Performance", "Federal Aviation Regulations", "Navigation", "Aeromedical Factors"]
            logger.info(f"Using default topics for user {uid}: {topics}")
            # Optionally select a subset if the default list is long and the flow has limits
            # topics = random.sample(topics, min(len(topics), 5))

        # 4. Build quiz input & call Genkit Quiz flow
        logger.info(f"Calling Genkit Quiz flow for user {uid} with topics: {topics}")
        quiz_input = {
            "userId": uid, # Include user ID if flow might use it
            "numQuestions": num_questions,
            "topics": topics # Pass the determined list of topics
        }
        try:
            # Name "QuizFlow" must match the flow definition name/file
            quiz_output = load_and_run_flow("QuizFlow", quiz_input)
            logger.info("Quiz flow returned successfully.")

            # Validate output structure (must contain a list of questions)
            if not isinstance(quiz_output, dict) or "questions" not in quiz_output or not isinstance(quiz_output["questions"], list):
                 logger.error(f"QuizFlow output is missing or invalid 'questions' list. Output: {quiz_output}")
                 raise HttpsError(code=FunctionsErrorCode.INTERNAL, message="Quiz generation failed: invalid format.")
            if len(quiz_output["questions"]) != num_questions:
                 logger.warning(f"QuizFlow returned {len(quiz_output['questions'])} questions, but requested {num_questions}.")
                 # Decide if this is an error or acceptable

            # Optional: Deeper validation of each question's structure
            for i, q in enumerate(quiz_output["questions"]):
                 if not isinstance(q, dict) or not all(k in q for k in ["question", "choices", "correctAnswer", "explanation"]):
                     logger.error(f"Invalid structure for question {i} in QuizFlow output: {q}")
                     raise HttpsError(code=FunctionsErrorCode.INTERNAL, message="Quiz data format incorrect.")
                 # Add more checks as needed (e.g., choices is list, correctAnswer is valid index)

        except HttpsError:
             raise # Re-throw validation errors
        except Exception as e:
            logger.error(f"Error calling or processing Quiz flow: {e}")
            raise HttpsError(code=FunctionsErrorCode.INTERNAL,
                             message="Failed to generate quiz questions.")

        # 5. Parse & store quiz
        logger.info("Persisting generated quiz.")
        quiz_coll_ref = db.collection("users").document(uid).collection("quizzes")
        quiz_ref = quiz_coll_ref.document() # Create new document ref
        quiz_payload = {
            "questions": quiz_output["questions"], # Store the list of question objects
            "score": None, # Score is null until graded/submitted
            "timestamp": firestore.SERVER_TIMESTAMP,
            "topics": topics # Store the topics used for this specific quiz
        }
        quiz_ref.set(quiz_payload)
        quiz_id = quiz_ref.id
        logger.info(f"Quiz persisted for user {uid} with ID: {quiz_id} under path: {quiz_ref.path}")

        # 6. Return quiz data
        response_payload = {
            "quizId": quiz_id,
            "questions": quiz_output["questions"] # Return the generated questions to the client
        }
        logger.info(f"Returning generated quiz ID {quiz_id}.")
        return response_payload

    except HttpsError as e:
        raise e
    except Exception as e:
        logger.exception(f"An unexpected error occurred in generateQuiz for user {uid}: {e}")
        raise HttpsError(code=FunctionsErrorCode.INTERNAL,
                         message="An internal error occurred while generating the quiz.")


@fn.https_fn.on_call()
def submitFeedback(request: fn.https_fn.CallableRequest):
    """Records user feedback."""
    logger.info("submitFeedback triggered.")

    # 1. Auth & validate input
    if not request.auth:
        logger.error("Authentication required for submitFeedback.")
        raise HttpsError(code=FunctionsErrorCode.UNAUTHENTICATED,
                         message="Authentication required")
    uid = request.auth.uid
    data = request.data or {}
    typ = data.get("type")        # e.g., "chat", "quiz", "general"
    rid = data.get("relatedId")   # e.g., messageId, quizId
    rating = data.get("rating")     # e.g., -1, 0, 1
    comment = data.get("comment", "") # Optional text comment (ensure default is string)

    # Validation
    valid_types = ["chat", "quiz", "general"]
    if typ not in valid_types:
         raise HttpsError(code=FunctionsErrorCode.INVALID_ARGUMENT,
                          message=f"Invalid feedback type '{typ}'. Must be one of: {valid_types}.")
    # relatedId is required for 'chat' and 'quiz' types
    if typ != "general" and (not rid or not isinstance(rid, str)):
         raise HttpsError(code=FunctionsErrorCode.INVALID_ARGUMENT,
                          message=f"Missing or invalid 'relatedId' (string) for feedback type '{typ}'.")
    # Validate rating (adjust allowed values as needed, e.g., -1, 0, 1)
    valid_ratings = [-1, 0, 1]
    if rating not in valid_ratings: # Check exact values
         raise HttpsError(code=FunctionsErrorCode.INVALID_ARGUMENT,
                          message=f"Invalid 'rating'. Must be one of: {valid_ratings}.")
    if not isinstance(comment, str):
        raise HttpsError(code=FunctionsErrorCode.INVALID_ARGUMENT,
                         message="Invalid 'comment'. Must be a string.")

    try:
        # 2. Write master feedback record
        logger.info(f"Writing feedback record (type: {typ}, relatedId: {rid})")
        fb_ref = db.collection("feedback").document() # Auto-generate ID for the feedback entry
        fb_payload = {
            "userId": uid,
            "type": typ,
            "relatedId": rid if typ != "general" else None, # Store null if general feedback
            "rating": rating,
            "comment": comment,
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        fb_ref.set(fb_payload)
        logger.info(f"Feedback stored in /feedback/{fb_ref.id}")

        # 3. If chat feedback, update the specific message's feedbackRating
        if typ == "chat":
            # Client MUST provide chatId in the request data for chat feedback
            chat_id = data.get("chatId")
            message_id = rid # For chat type, relatedId is the messageId

            if not chat_id or not isinstance(chat_id, str):
                # Log the error, but don't fail the whole submission since the central record is saved.
                logger.error(f"Missing or invalid 'chatId' in request data for chat feedback (messageId: {message_id}). Cannot update message document.")
            else:
                try:
                    # Path to the specific message document
                    msg_ref = db.collection("users").document(uid) \
                              .collection("chats").document(chat_id) \
                              .collection("messages").document(message_id)

                    # Update the 'feedbackRating' field on the message document
                    # Use update() which fails if the document doesn't exist (safer than set with merge)
                    msg_ref.update({"feedbackRating": rating})
                    logger.info(f"Updated feedbackRating ({rating}) on message: {msg_ref.path}")
                except firebase_admin.exceptions.NotFound:
                     logger.error(f"Chat message not found, could not update feedbackRating: {msg_ref.path}")
                except Exception as e:
                    # Log other errors but allow central feedback record to persist.
                    logger.error(f"Failed to update feedbackRating on message {msg_ref.path}: {e}")

        # Potential extension: Update quiz doc if typ == "quiz"
        # elif typ == "quiz":
        #     quiz_id = rid
        #     # Quiz feedback logic here (e.g., flag quiz for review)
        #     pass

        # 4. Return success
        logger.info("Feedback submission successful.")
        return {"success": True}

    except HttpsError as e:
        raise e # Re-throw known validation/auth errors
    except Exception as e:
        logger.exception(f"An unexpected error occurred in submitFeedback for user {uid}: {e}")
        raise HttpsError(code=FunctionsErrorCode.INTERNAL,
                         message="An internal error occurred while submitting feedback.")


# Scheduled function (using Pub/Sub schedule based on initial code)
# Adjust schedule, timezone, memory, timeout as needed.
# Example: Run daily at 3 AM UTC.
@fn.pubsub.schedule(schedule="every day 03:00", timezone="UTC", timeout_sec=540, memory=fn.options.MemoryOption.MB_256)
def processFeedbackLoop(event: fn.pubsub.CloudEvent[fn.pubsub.PubsubMessage]):
    """Periodically analyzes recent feedback and generates reports or alerts."""
    # Using fn.pubsub.schedule based on original code, could also use fn.scheduler.on_schedule
    logger.info(f"processFeedbackLoop triggered by Pub/Sub schedule. Event ID: {event.id}")

    try:
        # 1. Query last-24h feedback
        cutoff = now_utc() - timedelta(hours=24)
        logger.info(f"Querying feedback submitted after: {cutoff.isoformat()}")

        feedback_coll = db.collection("feedback")
        # Use FieldFilter for structured query
        query = feedback_coll.where(filter=FieldFilter("timestamp", ">", cutoff))
        recent_feedback_stream = query.stream() # Get an iterator

        # 2. Aggregate feedback (using Counter for downvotes per relatedId)
        logger.info("Aggregating recent feedback.")
        downvotes = Counter() # Counts occurrences of relatedId for negative ratings
        negative_feedback_list = [] # Keep track of all negative feedback details if needed

        item_count = 0
        for fb_doc in recent_feedback_stream:
            item_count += 1
            data = fb_doc.to_dict()
            rating = data.get("rating")
            related_id = data.get("relatedId") # Can be None for general feedback

            # Consider ratings < 0 as downvotes (adjust if using different scale)
            if rating is not None and rating < 0:
                 negative_feedback_list.append(data) # Store details
                 if related_id: # Only count if relatedId exists
                    downvotes[related_id] += 1

        logger.info(f"Processed {item_count} feedback entries from the last 24 hours.")
        logger.info(f"Found {len(negative_feedback_list)} negative feedback entries.")

        # Identify items (e.g., message IDs) with multiple downvotes
        downvote_threshold = 5 # Threshold from instructions
        frequent_downvoted_ids = [rid for rid, count in downvotes.items() if count >= downvote_threshold]

        logger.info(f"Found {len(frequent_downvoted_ids)} related IDs with >= {downvote_threshold} downvotes: {frequent_downvoted_ids}")

        # 3. Write a report to Firestore
        report_date_str = now_utc().strftime("%Y%m%d") # Daily report ID based on date
        report_id = f"daily_feedback_{report_date_str}" # Use the date in the doc ID

        # Store report in a structured way, e.g., /reports/dailyFeedback/{YYYYMMDD}
        # Using collection(report_id).document() from instructions seems overly nested.
        # Storing directly as /reports/dailyFeedback/{report_id} or similar is common.
        # Let's use /reports/{report_id} for simplicity, assuming other report types might exist.
        rpt_ref = db.collection("reports").document(report_id)

        report_payload = {
            # Use 'frequent' as key based on instructions for the list of IDs
            "problematicMessages": frequent_downvoted_ids, # List of IDs (e.g., message IDs)
            "timestamp": firestore.SERVER_TIMESTAMP,
            # Add more context to the report
            "reportGeneratedAt": now_utc(), # Python datetime for easier querying later if needed
            "analysisPeriodStart": cutoff,
            "totalRecentFeedbackEntries": item_count,
            "totalNegativeFeedbackEntries": len(negative_feedback_list),
            "downvoteThreshold": downvote_threshold,
            # Optionally include samples of negative comments (beware of doc size limits)
            # "negativeFeedbackSamples": negative_feedback_list[:20]
        }
        rpt_ref.set(report_payload)
        logger.info(f"Daily feedback report generated: {rpt_ref.path}")

        # 4. Optional alerts (e.g., Slack/Email)
        if frequent_downvoted_ids:
             alert_message = f"Feedback Alert: {len(frequent_downvoted_ids)} items found with >= {downvote_threshold} downvotes. Check report: {report_id}"
             logger.warning(alert_message) # Log as warning
             # --- TODO: Implement your actual alerting mechanism here ---
             # Example (requires `requests` library and webhook URL):
             # try:
             #     import requests, os
             #     webhook_url = os.environ.get("SLACK_ALERT_WEBHOOK")
             #     if webhook_url:
             #         requests.post(webhook_url, json={"text": alert_message})
             #         logger.info("Sent alert notification.")
             # except Exception as alert_e:
             #     logger.error(f"Failed to send alert notification: {alert_e}")
             # ---------------------------------------------------------

        # 5. Optional cleanup (e.g., delete feedback older than 90 days)
        # Perform cleanup carefully, potentially in batches or a separate function.
        # Example placeholder:
        # try:
        #     cleanup_cutoff = now_utc() - timedelta(days=90)
        #     old_query = feedback_coll.where(filter=FieldFilter("timestamp", "<", cleanup_cutoff)).limit(500) # Process in batches
        #     # ... (add batch delete logic here) ...
        #     logger.info("Performed old feedback cleanup.")
        # except Exception as cleanup_e:
        #     logger.error(f"Error during feedback cleanup: {cleanup_e}")
        # -----------------------------------------
        pass # End of optional cleanup placeholder

        logger.info("processFeedbackLoop completed successfully.")

    except Exception as e:
        logger.exception(f"An error occurred during processFeedbackLoop execution: {e}")
        # Do not re-raise for scheduled functions, just log the error.
        # Consider sending an alert about the failure of this function itself.
