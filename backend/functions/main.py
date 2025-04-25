import firebase_functions as fn
import firebase_admin
from firebase_admin import firestore, auth as firebase_auth
from datetime import datetime, timezone, timedelta
import logging
import random

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
    logger.info(f"chatWithAI triggered with data: {request.data}")

    # 1. Auth & input validation
    if request.auth is None:
        logger.error("Authentication required.")
        raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.UNAUTHENTICATED,
                                     message="The function must be called while authenticated.")
    uid = request.auth.uid
    message_text = request.data.get("message")
    chat_id = request.data.get("chatId") # Optional

    if not message_text or not isinstance(message_text, str) or len(message_text.strip()) == 0:
        logger.error("Invalid message text provided.")
        raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                                     message='The function must be called with a valid "message" argument.')
    
    if chat_id and not isinstance(chat_id, str):
        logger.error("Invalid chatId provided.")
        raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                                     message='If provided, "chatId" must be a string.')

    try:
        user_chat_collection = db.collection("users").document(uid).collection("chats")

        # 2. Create or lookup chat
        if not chat_id:
            new_chat_ref = user_chat_collection.document()
            chat_id = new_chat_ref.id
            logger.info(f"Creating new chat with ID: {chat_id} for user: {uid}")
            new_chat_ref.set({
                "startTime": now_utc(),
                "topicGuess": None # Or potentially try a quick guess here?
            })
        else:
            # Validate chat_id exists (optional but good practice)
            chat_ref = user_chat_collection.document(chat_id)
            if not chat_ref.get().exists:
                 logger.error(f"Chat with ID {chat_id} not found for user {uid}.")
                 raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.NOT_FOUND,
                                              message=f"Chat with ID {chat_id} not found.")
            logger.info(f"Continuing chat with ID: {chat_id} for user: {uid}")

        chat_messages_collection = user_chat_collection.document(chat_id).collection("messages")

        # 3. Store user’s message
        logger.info("Storing user message.")
        user_message_ref = chat_messages_collection.document()
        user_message_ref.set({
            "sender": "user",
            "text": message_text,
            "timestamp": now_utc(),
            "sources": [],
            "feedbackRating": None
        })

        # 4. Fetch context
        logger.info("Fetching context for RAG flow.")
        context_summary = "" # Default empty string
        try:
            context_summary_ref = db.collection("users").document(uid).collection("state").document("contextSummary") # Adjusted path
            context_summary_doc = context_summary_ref.get()
            if context_summary_doc.exists:
                context_summary = context_summary_doc.to_dict().get("summary", "")
                logger.info("Found existing context summary.")
            else:
                logger.info("No existing context summary found.")
        except Exception as e:
            logger.warning(f"Could not fetch context summary for user {uid}: {e}")

        # Fetch recent messages (e.g., last 5 turns = 10 messages)
        recent_messages_data = []
        try:
            # Query last 10 messages (user + AI), ordered by timestamp
            messages_query = chat_messages_collection.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(10)
            docs = messages_query.stream()
            # We need them in chronological order for the RAG flow
            recent_messages_data = sorted(
                [{ "sender": doc.get("sender"), "text": doc.get("text") } for doc in docs if doc.exists],
                key=lambda x: messages_query.stream().__next__().get("timestamp") # Approximation, Firestore doesn't guarantee order preservation after list conversion
                # Correct way requires getting timestamps and sorting after fetching
            )
            # Proper sorting after fetch:
            fetched_docs = [(doc.get("timestamp"), { "sender": doc.get("sender"), "text": doc.get("text") }) for doc in messages_query.stream() if doc.exists]
            fetched_docs.sort(key=lambda x: x[0]) # Sort by timestamp (oldest first)
            recent_messages_data = [item[1] for item in fetched_docs]

            logger.info(f"Fetched {len(recent_messages_data)} recent messages.")
        except Exception as e:
            logger.warning(f"Could not fetch recent messages for chat {chat_id}: {e}")

        # 5. Call Genkit RAG flow
        logger.info("Calling Genkit RAG flow.")
        rag_input = {
            "userId": uid,
            "message": message_text,
            "contextSummary": context_summary,
            "recentMessages": recent_messages_data,
            # Add other relevant data if your flow expects it
        }
        
        try:
            # Use the imported function
            rag_output = load_and_run_flow("RAGFlow", rag_input)
            logger.info(f"RAG flow returned successfully. Output keys: {list(rag_output.keys())}")
            
            # Basic validation of expected output
            if "text" not in rag_output or "sources" not in rag_output:
                logger.error(f"RAG flow output is missing expected keys ('text', 'sources'). Output: {rag_output}")
                raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INTERNAL, message="RAG flow returned invalid data.")

        except Exception as e:
            logger.error(f"Error calling RAG flow: {e}")
            # Decide: return generic error or specific based on e?
            raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INTERNAL, 
                                         message="Failed to get response from AI model.")

        # 6. Persist AI response
        logger.info("Storing AI response.")
        ai_message_ref = chat_messages_collection.document()
        ai_message_payload = {
            "sender": "ai",
            "text": rag_output["text"],
            "sources": rag_output["sources"], # Ensure this is Firestore-compatible (e.g., list of dicts)
            "timestamp": now_utc(),
            "feedbackRating": None
        }
        ai_message_ref.set(ai_message_payload)
        logger.info(f"AI response stored with ID: {ai_message_ref.id}")

        # 7. Return payload
        response_payload = {
            "chatId": chat_id,
            "messageId": ai_message_ref.id,
            "responseText": rag_output["text"],
            "sources": rag_output["sources"]
        }
        logger.info(f"Returning response payload: {response_payload}")
        return response_payload

    except fn.https_fn.HttpsError as e:
        # Re-throw HttpsErrors directly
        raise e
    except Exception as e:
        logger.exception(f"An unexpected error occurred in chatWithAI for user {uid}: {e}") # Log full traceback
        raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INTERNAL, 
                                     message="An internal error occurred.")


@fn.https_fn.on_call(max_instances=5)
def generateQuiz(request: fn.https_fn.CallableRequest):
    """Generates a quiz, potentially based on user progress."""
    logger.info(f"generateQuiz triggered with data: {request.data}")

    # 1. Auth & input validation
    if request.auth is None:
        logger.error("Authentication required for generateQuiz.")
        raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.UNAUTHENTICATED,
                                     message="The function must be called while authenticated.")
    uid = request.auth.uid
    num_questions = request.data.get("numQuestions", 5) # Default to 5 questions
    topic_filters = request.data.get("topicFilters") # Optional list of topics

    # Validate inputs
    if not isinstance(num_questions, int) or num_questions <= 0 or num_questions > 20: # Add reasonable limits
         logger.error(f"Invalid numQuestions: {num_questions}")
         raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                                      message="'numQuestions' must be a positive integer (max 20)." )
    if topic_filters and not isinstance(topic_filters, list):
        logger.error(f"Invalid topicFilters: {topic_filters}")
        raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                                     message="'topicFilters' must be a list of strings." )

    try:
        # 2. Gather user state / determine topics
        selected_topics = []
        if topic_filters:
            logger.info(f"Using provided topic filters: {topic_filters}")
            selected_topics = topic_filters
        else:
            # Try fetching weak topics
            try:
                # TODO: Manually update this path if your weak topics are stored elsewhere
                weak_topics_ref = db.collection("users").document(uid).collection("progress").document("weakTopics")
                weak_topics_doc = weak_topics_ref.get()
                if weak_topics_doc.exists:
                    weak_topics_data = weak_topics_doc.to_dict().get("topics", []) # Assuming structure { topics: [...] }
                    if weak_topics_data:
                        # Sample from weak topics if available
                        # Simple sampling: take up to 5 random weak topics
                        sample_size = min(len(weak_topics_data), 5) # Limit sample size
                        selected_topics = random.sample(weak_topics_data, sample_size)
                        logger.info(f"Sampled weak topics: {selected_topics}")
            except Exception as e:
                logger.warning(f"Could not fetch or sample weak topics for user {uid}: {e}")
        
        # Fallback to default topics if none selected
        if not selected_topics:
             # TODO: Manually define your default FAA-style topics here
            default_topics = ["Airspace", "Weather", "Regulations", "Aerodynamics", "Navigation"]
            selected_topics = random.sample(default_topics, min(len(default_topics), 3)) # Sample a few defaults
            logger.info(f"Falling back to default topics: {selected_topics}")


        # 3. & 4. Compose prompt and call LLM (using Genkit flow)
        logger.info(f"Generating {num_questions} questions on topics: {selected_topics}")
        quiz_input = {
            "userId": uid, # Optional, but potentially useful for personalization
            "numQuestions": num_questions,
            "topics": selected_topics
        }
        
        try:
            # TODO: Ensure you have a "QuizFlow" defined in backend/genkit/quiz_flow.json
            # And that load_and_run_flow in config.py handles it.
            quiz_output = load_and_run_flow("QuizFlow", quiz_input)
            logger.info("QuizFlow executed successfully.")
            
            # Validate output structure (adjust based on your actual QuizFlow output)
            if "questions" not in quiz_output or not isinstance(quiz_output["questions"], list):
                 logger.error(f"QuizFlow output missing or invalid 'questions' list. Output: {quiz_output}")
                 raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INTERNAL, message="Quiz generation failed.")
            
            # Further validation of question structure (optional but recommended)
            for q in quiz_output["questions"]:
                if not all(k in q for k in ["question", "choices", "correctAnswer", "explanation"]):
                    logger.error(f"Invalid question structure in QuizFlow output: {q}")
                    raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INTERNAL, message="Quiz data format incorrect.")

        except fn.https_fn.HttpsError: # Re-raise validation errors
             raise
        except Exception as e:
            logger.error(f"Error calling QuizFlow: {e}")
            raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INTERNAL, message="Failed to generate quiz questions.")

        # 5. Parse & persist quiz
        logger.info("Persisting generated quiz.")
        quiz_collection = db.collection("users").document(uid).collection("quizzes")
        new_quiz_ref = quiz_collection.document()
        quiz_payload = {
            "questions": quiz_output["questions"], # Assumes flow returns the correct structure
            "score": None,
            "timestamp": now_utc(),
            "topic": selected_topics # Record the topics used for this quiz
        }
        new_quiz_ref.set(quiz_payload)
        quiz_id = new_quiz_ref.id
        logger.info(f"Quiz persisted with ID: {quiz_id}")

        # 6. Return
        response_payload = {
            "quizId": quiz_id,
            "questions": quiz_output["questions"]
        }
        logger.info("Returning generated quiz.")
        return response_payload

    except fn.https_fn.HttpsError as e:
        raise e
    except Exception as e:
        logger.exception(f"An unexpected error occurred in generateQuiz for user {uid}: {e}")
        raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INTERNAL,
                                     message="An internal error occurred while generating the quiz.")


@fn.https_fn.on_call()
def submitFeedback(request: fn.https_fn.CallableRequest):
    """Records user feedback on chat messages or quizzes."""
    logger.info(f"submitFeedback triggered with data: {request.data}")

    # 1. Auth & input validation
    if request.auth is None:
        logger.error("Authentication required for submitFeedback.")
        raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.UNAUTHENTICATED,
                                     message="The function must be called while authenticated.")
    uid = request.auth.uid
    feedback_type = request.data.get("type")
    related_id = request.data.get("relatedId")
    rating = request.data.get("rating")
    comment = request.data.get("comment", "") # Optional comment

    # Validate inputs
    valid_types = ["chat", "quiz", "general"]
    if feedback_type not in valid_types:
        logger.error(f"Invalid feedback type: {feedback_type}")
        raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                                     message=f"'type' must be one of {valid_types}." )
    if not related_id or not isinstance(related_id, str):
         # Allow general feedback without relatedId? Maybe type "general" doesn't need it.
         if feedback_type != "general":
             logger.error(f"Missing or invalid relatedId for type '{feedback_type}': {related_id}")
             raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                                          message="'relatedId' (string) is required for type 'chat' or 'quiz'." )
    # Allow rating to be float or int, check range
    if not isinstance(rating, (int, float)) or rating not in [-1, 0, 1]:
         logger.error(f"Invalid rating: {rating}")
         raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                                      message="'rating' must be -1, 0, or 1." )
    if not isinstance(comment, str):
         logger.error(f"Invalid comment type: {type(comment)}")
         raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                                      message="'comment' must be a string." )

    try:
        # 2. Write to central feedback collection
        logger.info(f"Writing feedback of type '{feedback_type}' to central collection.")
        feedback_collection = db.collection("feedback")
        feedback_doc_ref = feedback_collection.document() # Auto-generate ID
        feedback_payload = {
            "userId": uid,
            "type": feedback_type,
            "relatedId": related_id if feedback_type != "general" else None,
            "rating": rating,
            "comment": comment,
            "timestamp": now_utc()
        }
        feedback_doc_ref.set(feedback_payload)
        logger.info(f"Feedback stored in /feedback/{feedback_doc_ref.id}")

        # 3. If chat feedback, update the specific message
        if feedback_type == "chat":
            chat_id = request.data.get("chatId") # Need chatId to locate the message
            message_id = related_id # In chat context, relatedId is the messageId
            
            if not chat_id or not isinstance(chat_id, str):
                 logger.error("Missing chatId for chat feedback update.")
                 # Log error but don't necessarily fail the whole feedback submission
                 # Or raise HttpsError if this update is critical
                 # raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                 #                              message="'chatId' is required when submitting chat feedback." )
            else:
                try:
                    message_ref = db.collection("users").document(uid).collection("chats")\
                                    .document(chat_id).collection("messages").document(message_id)
                    
                    # Check if message exists before updating (optional but safer)
                    message_doc = message_ref.get()
                    if message_doc.exists:
                        message_ref.update({"feedbackRating": rating})
                        logger.info(f"Updated feedbackRating on chat message: /users/{uid}/chats/{chat_id}/messages/{message_id}")
                    else:
                         logger.warning(f"Chat message not found, could not update feedbackRating: /users/{uid}/chats/{chat_id}/messages/{message_id}")
                except Exception as e:
                    # Log error but potentially allow feedback to be stored centrally anyway
                    logger.error(f"Failed to update feedbackRating on chat message {message_id} in chat {chat_id}: {e}")
        
        # If quiz feedback, could potentially update the quiz doc, e.g., with average rating
        # elif feedback_type == "quiz":
            # quiz_id = related_id
            # # Update logic for quiz doc if needed
            # pass

        # 4. Return success
        logger.info("Feedback submission successful.")
        return { "success": True }

    except fn.https_fn.HttpsError as e:
        raise e
    except Exception as e:
        logger.exception(f"An unexpected error occurred in submitFeedback for user {uid}: {e}")
        raise fn.https_fn.HttpsError(code=fn.https_fn.FunctionsErrorCode.INTERNAL,
                                     message="An internal error occurred while submitting feedback.")


# Schedule: Run once daily (adjust cron string as needed)
# e.g., "every day 03:00" or "0 3 * * *"
@fn.pubsub.schedule(schedule="every 24 hours", timeout_sec=540, memory=fn.options.MemoryOption.MB_256)
def processFeedbackLoop(event: fn.pubsub.CloudEvent[fn.pubsub.PubsubMessage]):
    """Periodically analyzes recent feedback and generates reports or alerts."""
    logger.info(f"processFeedbackLoop triggered. Event ID: {event.id}")
    
    try:
        # 1. Query recent feedback
        cutoff_time = now_utc() - timedelta(hours=24)
        logger.info(f"Querying feedback submitted after: {cutoff_time.isoformat()}")
        
        feedback_collection = db.collection("feedback")
        query = feedback_collection.where(filter=firestore.FieldFilter("timestamp", ">", cutoff_time))
        recent_feedback_docs = list(query.stream()) # Execute query and get all docs
        
        logger.info(f"Found {len(recent_feedback_docs)} feedback entries in the last 24 hours.")
        if not recent_feedback_docs:
            logger.info("No recent feedback to process.")
            return # Exit early if nothing to do

        # 2. Aggregate & detect pain points
        # Example: Find chat messages with multiple downvotes
        downvoted_chat_messages = {}
        general_issues = []
        quiz_issues = []

        for doc in recent_feedback_docs:
            data = doc.to_dict()
            rating = data.get("rating")
            feedback_type = data.get("type")
            related_id = data.get("relatedId")

            if rating == -1: # Focus on negative feedback
                if feedback_type == "chat" and related_id:
                    if related_id not in downvoted_chat_messages:
                        downvoted_chat_messages[related_id] = []
                    downvoted_chat_messages[related_id].append(data) # Store full feedback data
                elif feedback_type == "quiz" and related_id:
                    quiz_issues.append(data)
                elif feedback_type == "general":
                    general_issues.append(data)
        
        # Filter for messages with multiple downvotes
        problematic_message_ids = {msg_id: feedbacks 
                                   for msg_id, feedbacks in downvoted_chat_messages.items() 
                                   if len(feedbacks) > 1} # Threshold: > 1 downvote
        
        logger.info(f"Found {len(problematic_message_ids)} chat messages with multiple downvotes.")
        logger.info(f"Found {len(quiz_issues)} downvoted quizzes.")
        logger.info(f"Found {len(general_issues)} general negative feedback entries.")

        # 3. Take action - Generate a report
        report_content = {
            "reportDate": now_utc().strftime("%Y-%m-%d"),
            "totalFeedbackEntries": len(recent_feedback_docs),
            "downvotedChatMessagesThreshold": 2,
            "problematicChatMessages": problematic_message_ids, # Contains lists of feedback dicts per message ID
            "downvotedQuizzes": quiz_issues,
            "generalNegativeFeedback": general_issues,
            "analysisTimestamp": now_utc()
        }

        report_date_str = now_utc().strftime("%Y%m%d")
        report_ref = db.collection("reports").document(f"dailyFeedback_{report_date_str}")
        report_ref.set(report_content)
        logger.info(f"Daily feedback report generated: /reports/dailyFeedback_{report_date_str}")

        # Optional: Update Genkit config (e.g., blacklist problematic chunks based on sources in feedback)
        # This would require parsing sources from the feedback data and mapping to Genkit config updates.
        # Example placeholder:
        # for msg_id, feedbacks in problematic_message_ids.items():
        #     for feedback in feedbacks:
        #         # Assuming original message data might be needed or stored with feedback
        #         # sources = find_sources_for_message(feedback["userId"], feedback["chatId"], msg_id) 
        #         # if sources:
        #         #     update_genkit_blacklist(sources) ...
        #     pass

        # Optional: Send notification (Email/Slack)
        # This requires setting up integrations (e.g., SendGrid, Slack webhook)
        # if problematic_message_ids or quiz_issues or general_issues:
        #     send_notification(f"Daily Feedback Alert: {len(problematic_message_ids)} problem messages found.")
        #     logger.info("Notification sent.")

        # 4. Clean up (Optional)
        # Example: Delete feedback older than 30 days
        # cutoff_delete = now_utc() - timedelta(days=30)
        # query_old = feedback_collection.where(filter=firestore.FieldFilter("timestamp", "<", cutoff_delete)).limit(500) # Process in batches
        # deleted_count = 0
        # for doc_to_delete in query_old.stream():
        #     doc_to_delete.reference.delete()
        #     deleted_count += 1
        # logger.info(f"Deleted {deleted_count} feedback entries older than 30 days.")

        logger.info("processFeedbackLoop completed successfully.")

    except Exception as e:
        logger.exception(f"An error occurred during processFeedbackLoop: {e}")
        # Don't re-raise for scheduled functions, just log the error.
        # Consider sending an alert about the failure itself.
