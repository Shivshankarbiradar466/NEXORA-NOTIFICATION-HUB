from pathlib import Path
import joblib
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

data=[("Interview scheduled for software engineer","HR"),("Please submit your leave request","HR"),("Urgent production issue requires attention","Urgent"),("Customer complaint about service","Complaint"),("Invoice payment confirmation","Finance"),("Payment due for invoice","Finance"),("Team meeting at 10 AM","Work"),("Project meeting agenda","Work"),("General information request","General"),("Hello, I wanted to share some information","General")]
texts,labels=zip(*data)
model=Pipeline([("tfidf",TfidfVectorizer(ngram_range=(1,2),min_df=1)),("clf",LogisticRegression(max_iter=1000))])
model.fit(texts,labels)
out=Path("app/ml/email_classifier.joblib"); out.parent.mkdir(parents=True,exist_ok=True); joblib.dump(model,out); print("Saved:",out)
