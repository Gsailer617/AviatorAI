import firebase_functions as fn
import firebase_admin
from firebase_admin import firestore
import genkit

firebase_admin.initialize_app()
db = firestore.client()

@fn.https_fn.on_call()
def chatWithAI(request):
    # TODO: RAG retrieval, Genkit flow, store messages
    pass

@fn.https_fn.on_call()
def generateQuiz(request):
    # TODO: adaptive quiz generation
    pass

@fn.https_fn.on_call()
def submitFeedback(request):
    # TODO: record feedback
    pass

@fn.pubsub.schedule("every 24 hours").on_run()
def processFeedbackLoop(event):
    # TODO: analyze feedback, report/log
    pass
