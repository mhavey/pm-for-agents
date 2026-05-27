aws ecr get-login-password --region $AWS_REGION | \
docker login --username AWS --password-stdin ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

# 2. Build the image
docker build -t pmagent/research_graph .

# 3. Tag the image for the remote repository
docker tag pmagent/research_graph:latest ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/pmagent/research_graph:latest

# 4. Push to ECR
docker push ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/pmagent/research_graph:latest

