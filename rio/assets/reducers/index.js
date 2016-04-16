import { combineReducers } from 'redux'
import projects from './projects'

const rio = combineReducers({
  projects: projects
})

export default rio
