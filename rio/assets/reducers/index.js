import { combineReducers } from 'redux'
import { reducer as form } from 'redux-form'
import { routerReducer } from 'react-router-redux'
import projects from './projects'

const reducers = {
  routing: routerReducer,
  form,
  projects
}

const rio = combineReducers(reducers)

export default rio
