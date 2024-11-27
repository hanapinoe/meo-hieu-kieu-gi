from flask import Flask, request, jsonify, render_template
import torch
import torchvision.transforms as transforms
from torchvision.models import resnet50, ResNet50_Weights
from PIL import Image
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from pymongo import MongoClient
import os
import joblib
import pytesseract  # Thư viện OCR
from flask_cors import CORS

# Đặt đường dẫn đến tesseract
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# Khởi tạo Flask app
app = Flask(__name__)
CORS(app)  # Cho phép CORS

# Kết nối với MongoDB
client = MongoClient('mongodb://localhost:27017/')
db = client['bookstore']
book_collection = db['books']

# Tải mô hình Siamese đã huấn luyện
class SiameseNetwork(torch.nn.Module):
    def __init__(self, img_embedding_dim, text_embedding_dim, output_dim=128):
        super(SiameseNetwork, self).__init__()
        self.img_transform = torch.nn.Linear(img_embedding_dim, output_dim)
        self.text_transform = torch.nn.Linear(text_embedding_dim, output_dim)
        self.shared_net = torch.nn.Sequential(
            torch.nn.Linear(output_dim, 128),
            torch.nn.ReLU(),
            torch.nn.Linear(128, 64),
            torch.nn.ReLU(),
            torch.nn.Linear(64, 32),
            torch.nn.ReLU()
        )

    def forward_once(self, x):
        return self.shared_net(x)

    def forward(self, img_input, text_input):
        img_embedding = self.img_transform(img_input)
        text_embedding = self.text_transform(text_input)
        output1 = self.forward_once(img_embedding)
        output2 = self.forward_once(text_embedding)
        return output1, output2

# Tải mô hình và vectorizer
img_embedding_dim = 2048
text_embedding_dim = 101
model = SiameseNetwork(img_embedding_dim, text_embedding_dim, output_dim=128)
model.load_state_dict(torch.load('siamese_model.pth'))
model.eval()

vectorizer = joblib.load('vectorizer.pkl')

# Tiền xử lý ảnh
def preprocess_image(image_path):
    transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    image = Image.open(image_path).convert('RGB')
    return transform(image).unsqueeze(0)

# Trích xuất văn bản từ ảnh bằng OCR
def extract_text_from_image(image_path):
    image = Image.open(image_path)
    extracted_text = pytesseract.image_to_string(image)
    return extracted_text.strip()

# Tạo embedding cho văn bản
def create_text_embedding(title):
    text_embedding = vectorizer.transform([title]).toarray()
    text_embedding = torch.tensor(text_embedding, dtype=torch.float)
    text_embedding = model.text_transform(text_embedding)
    text_embedding = model.forward_once(text_embedding)
    return text_embedding.detach().numpy().reshape(1, -1)  # Chuyển thành mảng hai chiều

# Tạo embedding cho ảnh chỉ với một đầu vào
def create_image_embedding(image_path):
    # Tải mô hình ResNet50 đã được huấn luyện trước
    resnet_model = resnet50(weights=ResNet50_Weights.IMAGENET1K_V1)
    resnet_model = torch.nn.Sequential(*list(resnet_model.children())[:-1])  # Lấy layer trước classifier
    resnet_model.eval()
    
    # Tiền xử lý ảnh
    transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    image = Image.open(image_path).convert('RGB')
    image_tensor = transform(image).unsqueeze(0)
    
    # Trích xuất đặc trưng từ ảnh
    with torch.no_grad():
        features = resnet_model(image_tensor)
    
    # Chuyển đổi kích thước của đặc trưng để phù hợp với mô hình SiameseNetwork
    img_embedding = features.view(features.size(0), -1)
    img_embedding = model.img_transform(img_embedding)
    img_embedding = model.forward_once(img_embedding)
    
    return img_embedding.detach().numpy().reshape(1, -1)  # Chuyển thành mảng hai chiều

@app.route('/')
def index():
    return render_template('frontend.html')

@app.route('/search', methods=['POST'])
def search_books():
    # Tạo thư mục tạm nếu chưa tồn tại
    if not os.path.exists('temp'):
        os.makedirs('temp')

    image = request.files.get('image')
    title = request.form.get('title')

    if not image and not title:
        return jsonify({"error": "Vui lòng cung cấp ảnh hoặc tiêu đề."}), 400

    input_embedding = None

    # Xử lý đầu vào từ người dùng
    if image:
        image_path = f"temp/{image.filename}"
        image.save(image_path)

        # Thử OCR trước
        try:
            extracted_text = extract_text_from_image(image_path)
            if extracted_text.strip():
                input_embedding = create_text_embedding(extracted_text)
            else:
                input_embedding = create_image_embedding(image_path)
        except Exception as e:
            return jsonify({"error": f"Lỗi xử lý ảnh: {str(e)}"}), 500

    elif title:
        try:
            input_embedding = create_text_embedding(title)
        except Exception as e:
            return jsonify({"error": f"Lỗi xử lý tiêu đề: {str(e)}"}), 500

    # Kiểm tra embedding đầu vào
    if input_embedding is None or input_embedding.shape[1] not in [101, 2048]:
        return jsonify({"error": "Embedding không hợp lệ."}), 400

    # Lấy tất cả sách từ MongoDB
    books = list(book_collection.find())
    results = []

    for book in books:
        # Lấy embedding từ MongoDB
        stored_embedding = book.get('image_embedding' if image else 'text_embedding')
        if not stored_embedding:
            continue

        embedding = np.array(stored_embedding).reshape(1, -1)

        # Kiểm tra kích thước embedding
        if embedding.shape[1] != input_embedding.shape[1]:
            print(f"Bỏ qua sách '{book['title']}' do không tương thích kích thước embedding.")
            continue

        # Tính cosine similarity
        try:
            similarity = cosine_similarity(input_embedding, embedding)[0][0]
        except Exception as e:
            print(f"Lỗi khi tính toán similarity cho sách '{book['title']}': {str(e)}")
            continue

        # Thêm sách vào kết quả
        results.append({
            "title": book["title"],
            "price": float(book["price"]),  # Đảm bảo giá trị price là float
            "image_url": book["image_url"],
            "similarity": float(similarity)  # Đảm bảo similarity là float
        })

    # Sắp xếp kết quả và trả về
    results = sorted(results, key=lambda x: x['similarity'], reverse=True)[:5]
    return jsonify(results)



if __name__ == '__main__':
    app.run(debug=True, port=5000)
